"""Build a CDM-resident executable cohort-query benchmark over OMOP CDM.

The generator first constructs a capability matrix from the connected OMOP CDM
and then emits only query patterns whose concepts and event combinations are
resident in that database.  The resulting benchmark is therefore an
executable-reference benchmark: it evaluates fidelity to predefined OMOP cohort
semantics, not chart-reviewed clinical phenotype validity.

The default configuration targets the SYN-SNUH synthetic OMOP CDM distributed
through KHDP. The method can be applied to another OMOP CDM, but aliases,
preferred pair lists, drug-class lists, and Tier-B negative-control candidates
should be reviewed against the new database's resident inventory.

Outputs
-------
- omop_benchmark_queries.jsonl
- omop_benchmark_mapping.csv
- omop_capability_matrix.json
- TSV capability tables under ``--out-dir``

The JSONL contains extra metadata fields; the existing batch runner only needs
``id`` and ``query`` and will ignore the rest.

Example
-------
python build_omop_executable_benchmark.py \
  --gold-source athena \
  --out-dir outputs/omop_executable_benchmark

Gold slots are Athena/OMOP concept-set seeds by default: ``athena:<concept_id>``.
The downstream gold script expands Tier-A seeds with ``concept_ancestor`` and
restricts them to concepts observed in the target CDM before count derivation.
Tier-B negative controls are emitted with empty gold slots and
zero-by-construction cohort counts after programmatic absence validation.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

DATASET_VERSION = "omop_executable_benchmark_resident_tierb_2023_2024"
DEFAULT_SCHEMA = "synthetic_snuh_cdm"
DEFAULT_VOCAB_SCHEMA = "public"
DEFAULT_CONFIG_PATH = Path(__file__).with_name("syn_snuh_benchmark_config.json")

MALE_CONCEPT_ID = 8507
FEMALE_CONCEPT_ID = 8532

CONDITION_ALIASES: Mapping[str, Sequence[str]] = {}
DRUG_ALIASES: Mapping[str, Sequence[str]] = {}
DRUG_CLASS_ALIASES: Mapping[str, Sequence[str]] = {}
L4_PREFERRED_DRUGS: Mapping[str, Sequence[str]] = {}
L5_PREFERRED_CLASSES: Mapping[str, Sequence[str]] = {}
QUERY_VARIANTS_SINGLE: Sequence[str] = ()
NEGATIVE_CONTROL_QUERIES: Sequence[Mapping[str, str]] = ()
NEGATIVE_CONTROL_ABSENCE_TERMS: Mapping[str, Sequence[str]] = {}


def _string_sequence_map(raw: Any, key: str) -> Dict[str, Tuple[str, ...]]:
    if not isinstance(raw, dict):
        raise SystemExit(f"config field {key!r} must be an object")
    out: Dict[str, Tuple[str, ...]] = {}
    for name, values in raw.items():
        if not isinstance(name, str) or not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise SystemExit(f"config field {key!r} must map strings to string lists")
        out[name] = tuple(values)
    return out


def load_benchmark_config(path: Path) -> None:
    """Load dataset-specific aliases and query candidates from JSON config."""
    global CONDITION_ALIASES, DRUG_ALIASES, DRUG_CLASS_ALIASES
    global L4_PREFERRED_DRUGS, L5_PREFERRED_CLASSES, QUERY_VARIANTS_SINGLE
    global NEGATIVE_CONTROL_QUERIES, NEGATIVE_CONTROL_ABSENCE_TERMS

    if not path.is_file():
        raise SystemExit(f"benchmark config not found: {path}")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise SystemExit("benchmark config must be a JSON object")

    CONDITION_ALIASES = _string_sequence_map(cfg.get("condition_aliases"), "condition_aliases")
    DRUG_ALIASES = _string_sequence_map(cfg.get("drug_aliases"), "drug_aliases")
    DRUG_CLASS_ALIASES = _string_sequence_map(cfg.get("drug_class_aliases"), "drug_class_aliases")
    L4_PREFERRED_DRUGS = _string_sequence_map(cfg.get("l4_preferred_drugs"), "l4_preferred_drugs")
    L5_PREFERRED_CLASSES = _string_sequence_map(cfg.get("l5_preferred_classes"), "l5_preferred_classes")
    NEGATIVE_CONTROL_ABSENCE_TERMS = _string_sequence_map(
        cfg.get("negative_control_absence_terms"),
        "negative_control_absence_terms",
    )

    qv = cfg.get("query_variants_single")
    if not isinstance(qv, list) or not qv or not all(isinstance(v, str) for v in qv):
        raise SystemExit("config field 'query_variants_single' must be a non-empty string list")
    QUERY_VARIANTS_SINGLE = tuple(qv)

    nq = cfg.get("negative_control_queries")
    if not isinstance(nq, list):
        raise SystemExit("config field 'negative_control_queries' must be a list")
    neg_rows: List[Mapping[str, str]] = []
    for row in nq:
        if not isinstance(row, dict) or not isinstance(row.get("category"), str) or not isinstance(row.get("query"), str):
            raise SystemExit("each negative_control_queries row must contain string fields 'category' and 'query'")
        neg_rows.append({"category": row["category"], "query": row["query"]})
    NEGATIVE_CONTROL_QUERIES = tuple(neg_rows)


def norm_text(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def strip_sql_semicolon(sql: str) -> str:
    return sql.strip().rstrip(";")


def sql_literal(value: Any) -> str:
    """Return a PostgreSQL single-quoted string literal for inline metadata SELECTs."""
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def make_db() -> Any:
    """Instantiate the repository DB connector lazily so --help works anywhere."""
    try:
        from config_loader import ConfigLoader  # type: ignore
        from db_connector import DBConnector  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on local repo
        raise SystemExit(
            "Could not import ConfigLoader/DBConnector. Run this script from the "
            "project repository where settings.yaml and db_connector.py exist."
        ) from exc
    return DBConnector(ConfigLoader())


def fetch_json_rows(db: Any, sql: str) -> List[Dict[str, Any]]:
    """Fetch a SELECT result as JSON using only DBConnector.fetch_scalar()."""
    wrapped = (
        "SELECT COALESCE(jsonb_agg(to_jsonb(q)), '[]'::jsonb)::text "
        f"FROM ({strip_sql_semicolon(sql)}) q"
    )
    raw = db.fetch_scalar(wrapped)
    if raw is None:
        return []
    if isinstance(raw, str):
        return json.loads(raw)
    if isinstance(raw, list):
        return [dict(x) for x in raw]
    # psycopg2 may return a Python object for json/jsonb in some configurations.
    try:
        return [dict(x) for x in raw]
    except TypeError as exc:
        raise TypeError(f"Unexpected fetch_scalar JSON payload type: {type(raw)!r}") from exc


def write_tsv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    # Keep a stable, broad column order.
    cols: List[str] = []
    for row in rows:
        for k in row.keys():
            if k not in cols:
                cols.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter="\t", extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in cols})


@dataclass
class ConditionInfo:
    concept_id: int
    concept_name: str
    canonical_name: str
    query_name: str
    patient_count: int
    record_count: int
    first_date: str
    last_date: str


@dataclass
class DrugInfo:
    concept_id: int
    concept_name: str
    canonical_name: str
    query_name: str
    patient_count: int
    record_count: int
    first_date: str
    last_date: str


@dataclass
class QueryRow:
    id: str
    query: str
    tier: str
    level: str
    pattern: str
    condition_name: str = ""
    condition_concept_id: Optional[int] = None
    drug_name: str = ""
    drug_concept_id: Optional[int] = None
    drug_class: str = ""
    drug_concept_ids: str = ""
    age_min: Optional[int] = None
    gender: str = ""
    gender_concept_id: Optional[int] = None
    year: Optional[int] = None
    window_days: Optional[int] = None
    requires_temporal: bool = False
    gold_temporal_direction: str = ""
    gold_temporal_window_days: Optional[int] = None
    requires_numeric: bool = False
    gold_numeric_threshold: Optional[float] = None
    gold_numeric_unit: str = ""
    codeset_ids: str = ""
    gold_slots: str = ""
    gold_source: str = "athena"
    selection_count_basis: Optional[int] = None
    notes: str = ""

    def to_json_obj(self) -> Dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, "")}


def canonical_condition_name(concept_name: str) -> str:
    n = norm_text(concept_name)
    for canonical, aliases in CONDITION_ALIASES.items():
        if any(norm_text(a) in n for a in aliases):
            return canonical
    return concept_name


def canonical_drug_name(concept_name: str) -> str:
    n = norm_text(concept_name)
    for canonical, aliases in DRUG_ALIASES.items():
        if any(norm_text(a) in n for a in aliases):
            return canonical
    return concept_name


def gold_condition_slot(cond: ConditionInfo, *, gold_source: str) -> str:
    """Return a machine-readable gold slot for a condition.

    The default gold source records the resident standard concept as an
    OMOP/Athena seed. The gold-count script expands descendants through
    concept_ancestor and intersects the result with resident concepts.
    """
    gs = (gold_source or "athena").strip().lower()
    if gs in {"athena", "atlas", "omop_desc"}:
        return f"athena:{cond.concept_id}"
    if gs in {"omop", "omop_direct", "direct"}:
        return f"omop:{cond.concept_id}"
    raise SystemExit(f"unknown --gold-source: {gold_source!r}")


def gold_concept_slot(ids: Iterable[int], *, gold_source: str) -> str:
    clean = sorted({int(x) for x in ids if x is not None})
    prefix = "athena" if (gold_source or "athena").strip().lower() in {"athena", "atlas", "omop_desc"} else "omop"
    return prefix + ":" + ",".join(str(x) for x in clean)


def collect_condition_inventory(db: Any, schema: str, vocab_schema: str) -> List[Dict[str, Any]]:
    return fetch_json_rows(
        db,
        f"""
        SELECT
            co.condition_concept_id::bigint AS concept_id,
            c.concept_name,
            c.domain_id,
            c.vocabulary_id,
            c.standard_concept,
            COUNT(DISTINCT co.person_id)::bigint AS patient_count,
            COUNT(*)::bigint AS record_count,
            MIN(co.condition_start_date)::text AS first_date,
            MAX(co.condition_start_date)::text AS last_date
        FROM {schema}.condition_occurrence co
        JOIN {vocab_schema}.concept c
          ON c.concept_id = co.condition_concept_id
        WHERE co.condition_concept_id <> 0
          AND c.domain_id = 'Condition'
          AND c.standard_concept = 'S'
          AND c.invalid_reason IS NULL
        GROUP BY co.condition_concept_id, c.concept_name, c.domain_id, c.vocabulary_id, c.standard_concept
        ORDER BY patient_count DESC, concept_name
        """,
    )


def collect_drug_inventory(db: Any, schema: str, vocab_schema: str) -> List[Dict[str, Any]]:
    return fetch_json_rows(
        db,
        f"""
        SELECT
            de.drug_concept_id::bigint AS concept_id,
            c.concept_name,
            c.domain_id,
            c.vocabulary_id,
            c.standard_concept,
            COUNT(DISTINCT de.person_id)::bigint AS patient_count,
            COUNT(*)::bigint AS record_count,
            MIN(de.drug_exposure_start_date)::text AS first_date,
            MAX(de.drug_exposure_start_date)::text AS last_date
        FROM {schema}.drug_exposure de
        JOIN {vocab_schema}.concept c
          ON c.concept_id = de.drug_concept_id
        WHERE de.drug_concept_id <> 0
          AND c.domain_id = 'Drug'
          AND c.standard_concept = 'S'
          AND c.invalid_reason IS NULL
          AND c.vocabulary_id IN ('RxNorm', 'RxNorm Extension')
        GROUP BY de.drug_concept_id, c.concept_name, c.domain_id, c.vocabulary_id, c.standard_concept
        ORDER BY patient_count DESC, concept_name
        """,
    )


def collect_demographic_capability(db: Any, schema: str, vocab_schema: str) -> List[Dict[str, Any]]:
    return fetch_json_rows(
        db,
        f"""
        WITH dx AS (
            SELECT condition_concept_id, person_id, MIN(condition_start_date)::date AS dx_date
            FROM {schema}.condition_occurrence
            WHERE condition_concept_id <> 0
            GROUP BY condition_concept_id, person_id
        ), base AS (
            SELECT
                dx.condition_concept_id,
                c.concept_name,
                dx.person_id,
                p.gender_concept_id,
                CASE WHEN p.gender_concept_id = {FEMALE_CONCEPT_ID} THEN 'female'
                     WHEN p.gender_concept_id = {MALE_CONCEPT_ID} THEN 'male'
                     ELSE 'unknown' END AS gender,
                (EXTRACT(YEAR FROM dx.dx_date)::int - p.year_of_birth)::int AS age_at_dx
            FROM dx
            JOIN {schema}.person p ON p.person_id = dx.person_id
            JOIN {vocab_schema}.concept c ON c.concept_id = dx.condition_concept_id
        )
        SELECT
            condition_concept_id::bigint AS condition_concept_id,
            concept_name,
            COUNT(*)::bigint AS all_patients,
            COUNT(*) FILTER (WHERE gender_concept_id = {FEMALE_CONCEPT_ID})::bigint AS female,
            COUNT(*) FILTER (WHERE gender_concept_id = {MALE_CONCEPT_ID})::bigint AS male,
            COUNT(*) FILTER (WHERE age_at_dx >= 40)::bigint AS age_ge_40,
            COUNT(*) FILTER (WHERE age_at_dx >= 50)::bigint AS age_ge_50,
            COUNT(*) FILTER (WHERE age_at_dx >= 60)::bigint AS age_ge_60,
            COUNT(*) FILTER (WHERE age_at_dx >= 65)::bigint AS age_ge_65,
            COUNT(*) FILTER (WHERE gender_concept_id = {FEMALE_CONCEPT_ID} AND age_at_dx >= 40)::bigint AS female_age_ge_40,
            COUNT(*) FILTER (WHERE gender_concept_id = {FEMALE_CONCEPT_ID} AND age_at_dx >= 50)::bigint AS female_age_ge_50,
            COUNT(*) FILTER (WHERE gender_concept_id = {FEMALE_CONCEPT_ID} AND age_at_dx >= 60)::bigint AS female_age_ge_60,
            COUNT(*) FILTER (WHERE gender_concept_id = {FEMALE_CONCEPT_ID} AND age_at_dx >= 65)::bigint AS female_age_ge_65,
            COUNT(*) FILTER (WHERE gender_concept_id = {MALE_CONCEPT_ID} AND age_at_dx >= 40)::bigint AS male_age_ge_40,
            COUNT(*) FILTER (WHERE gender_concept_id = {MALE_CONCEPT_ID} AND age_at_dx >= 50)::bigint AS male_age_ge_50,
            COUNT(*) FILTER (WHERE gender_concept_id = {MALE_CONCEPT_ID} AND age_at_dx >= 60)::bigint AS male_age_ge_60,
            COUNT(*) FILTER (WHERE gender_concept_id = {MALE_CONCEPT_ID} AND age_at_dx >= 65)::bigint AS male_age_ge_65
        FROM base
        GROUP BY condition_concept_id, concept_name
        ORDER BY all_patients DESC, concept_name
        """,
    )


def collect_year_capability(db: Any, schema: str, vocab_schema: str) -> List[Dict[str, Any]]:
    return fetch_json_rows(
        db,
        f"""
        WITH dx AS (
            SELECT condition_concept_id, person_id, MIN(condition_start_date)::date AS dx_date
            FROM {schema}.condition_occurrence
            WHERE condition_concept_id <> 0
            GROUP BY condition_concept_id, person_id
        )
        SELECT
            dx.condition_concept_id::bigint AS condition_concept_id,
            c.concept_name,
            EXTRACT(YEAR FROM dx.dx_date)::int AS dx_year,
            COUNT(DISTINCT dx.person_id)::bigint AS patient_count
        FROM dx
        JOIN {vocab_schema}.concept c ON c.concept_id = dx.condition_concept_id
        WHERE dx.dx_date >= DATE '2023-01-01'
          AND dx.dx_date < DATE '2025-01-01'
        GROUP BY dx.condition_concept_id, c.concept_name, EXTRACT(YEAR FROM dx.dx_date)::int
        ORDER BY condition_concept_id, dx_year
        """,
    )


def collect_condition_to_drug(db: Any, schema: str, vocab_schema: str) -> List[Dict[str, Any]]:
    return fetch_json_rows(
        db,
        f"""
        WITH dx AS (
            SELECT condition_concept_id, person_id, MIN(condition_start_date)::date AS dx_date
            FROM {schema}.condition_occurrence
            WHERE condition_concept_id <> 0
            GROUP BY condition_concept_id, person_id
        )
        SELECT
            dx.condition_concept_id::bigint AS condition_concept_id,
            cc.concept_name AS condition_name,
            de.drug_concept_id::bigint AS drug_concept_id,
            dc.concept_name AS drug_name,
            COUNT(DISTINCT dx.person_id) FILTER (
                WHERE de.drug_exposure_start_date >= dx.dx_date
                  AND de.drug_exposure_start_date <= dx.dx_date + INTERVAL '90 days'
            )::bigint AS n_90d,
            COUNT(DISTINCT dx.person_id) FILTER (
                WHERE de.drug_exposure_start_date >= dx.dx_date
                  AND de.drug_exposure_start_date <= dx.dx_date + INTERVAL '365 days'
            )::bigint AS n_365d,
            MIN(de.drug_exposure_start_date)::text AS first_drug_date,
            MAX(de.drug_exposure_start_date)::text AS last_drug_date
        FROM dx
        JOIN {schema}.drug_exposure de
          ON de.person_id = dx.person_id
         AND de.drug_exposure_start_date >= dx.dx_date
         AND de.drug_exposure_start_date <= dx.dx_date + INTERVAL '365 days'
        JOIN {vocab_schema}.concept cc ON cc.concept_id = dx.condition_concept_id
        JOIN {vocab_schema}.concept dc ON dc.concept_id = de.drug_concept_id
        WHERE de.drug_concept_id <> 0
          AND dc.domain_id = 'Drug'
          AND dc.standard_concept = 'S'
          AND dc.invalid_reason IS NULL
          AND dc.vocabulary_id IN ('RxNorm', 'RxNorm Extension')
          AND cc.domain_id = 'Condition'
          AND cc.standard_concept = 'S'
          AND cc.invalid_reason IS NULL
        GROUP BY dx.condition_concept_id, cc.concept_name, de.drug_concept_id, dc.concept_name
        ORDER BY n_365d DESC, condition_name, drug_name
        """,
    )


def collect_l5_not_capability(
    db: Any,
    schema: str,
    conditions: Sequence[ConditionInfo],
    drug_classes: Mapping[str, Sequence[DrugInfo]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for cond in conditions:
        for class_name, drugs in drug_classes.items():
            ids = sorted({d.concept_id for d in drugs})
            if not ids:
                continue
            ids_sql = ",".join(str(x) for x in ids)
            sql = f"""
                WITH dx AS (
                    SELECT person_id, MIN(condition_start_date)::date AS dx_date
                    FROM {schema}.condition_occurrence
                    WHERE condition_concept_id = {cond.concept_id}
                    GROUP BY person_id
                )
                SELECT
                    {cond.concept_id}::bigint AS condition_concept_id,
                    {sql_literal(cond.query_name)}::text AS condition_name,
                    {sql_literal(class_name)}::text AS drug_class,
                    {sql_literal(','.join(str(x) for x in ids))}::text AS drug_concept_ids,
                    COUNT(*)::bigint AS condition_patients,
                    COUNT(*) FILTER (
                        WHERE EXISTS (
                            SELECT 1
                            FROM {schema}.drug_exposure de
                            WHERE de.person_id = dx.person_id
                              AND de.drug_concept_id IN ({ids_sql})
                              AND de.drug_exposure_start_date >= dx.dx_date
                        )
                    )::bigint AS with_drug_after_dx,
                    COUNT(*) FILTER (
                        WHERE NOT EXISTS (
                            SELECT 1
                            FROM {schema}.drug_exposure de
                            WHERE de.person_id = dx.person_id
                              AND de.drug_concept_id IN ({ids_sql})
                              AND de.drug_exposure_start_date >= dx.dx_date
                        )
                    )::bigint AS without_drug_after_dx
                FROM dx
            """
            rows.extend(fetch_json_rows(db, sql))
    rows.sort(key=lambda r: (int(r.get("without_drug_after_dx") or 0), int(r.get("condition_patients") or 0)), reverse=True)
    return rows


def materialize_conditions(raw_rows: Sequence[Mapping[str, Any]]) -> List[ConditionInfo]:
    out: List[ConditionInfo] = []
    seen_canonical: set[str] = set()
    for r in raw_rows:
        cid = int(r["concept_id"])
        cname = str(r["concept_name"])
        canonical = canonical_condition_name(cname)
        # Keep every actual DB concept, but for the known 14 concepts this also
        # gives stable readable labels.
        query_name = canonical if canonical in CONDITION_ALIASES else cname
        cond = ConditionInfo(
            concept_id=cid,
            concept_name=cname,
            canonical_name=canonical,
            query_name=query_name,
            patient_count=int(r.get("patient_count") or 0),
            record_count=int(r.get("record_count") or 0),
            first_date=str(r.get("first_date") or ""),
            last_date=str(r.get("last_date") or ""),
        )
        out.append(cond)
        seen_canonical.add(canonical)
    return out


def materialize_drugs(raw_rows: Sequence[Mapping[str, Any]]) -> List[DrugInfo]:
    out: List[DrugInfo] = []
    for r in raw_rows:
        cname = str(r["concept_name"])
        canonical = canonical_drug_name(cname)
        out.append(
            DrugInfo(
                concept_id=int(r["concept_id"]),
                concept_name=cname,
                canonical_name=canonical,
                query_name=canonical if canonical in DRUG_ALIASES else cname,
                patient_count=int(r.get("patient_count") or 0),
                record_count=int(r.get("record_count") or 0),
                first_date=str(r.get("first_date") or ""),
                last_date=str(r.get("last_date") or ""),
            )
        )
    return out


def build_drug_lookup(drugs: Sequence[DrugInfo]) -> Dict[str, DrugInfo]:
    # Prefer the most prevalent concept for aliases that match multiple rows.
    ordered = sorted(drugs, key=lambda d: d.patient_count, reverse=True)
    lookup: Dict[str, DrugInfo] = {}
    for d in ordered:
        for key in {norm_text(d.canonical_name), norm_text(d.query_name), norm_text(d.concept_name)}:
            lookup.setdefault(key, d)
    return lookup


def resolve_drug_class(drugs: Sequence[DrugInfo]) -> Dict[str, List[DrugInfo]]:
    lookup = build_drug_lookup(drugs)
    out: Dict[str, List[DrugInfo]] = {}
    for class_name, aliases in DRUG_CLASS_ALIASES.items():
        found: List[DrugInfo] = []
        found_ids: set[int] = set()
        for alias in aliases:
            # First exact/canonical lookup.
            d = lookup.get(norm_text(alias))
            if d and d.concept_id not in found_ids:
                found.append(d)
                found_ids.add(d.concept_id)
                continue
            # Fallback substring over actual DB names.
            na = norm_text(alias)
            for cand in sorted(drugs, key=lambda x: x.patient_count, reverse=True):
                if na and na in norm_text(cand.concept_name) and cand.concept_id not in found_ids:
                    found.append(cand)
                    found_ids.add(cand.concept_id)
                    break
        if found:
            out[class_name] = found
    return out


def choose_l1(
    conditions: Sequence[ConditionInfo],
    target: int,
    gold_source: str,
    allow_repeat: bool,
    start_idx: int,
) -> Tuple[List[QueryRow], int]:
    rows: List[QueryRow] = []
    ordered = sorted(conditions, key=lambda c: c.patient_count, reverse=True)
    i = 0
    while len(rows) < target and ordered:
        if i >= len(ordered):
            if not allow_repeat:
                break
            cond = ordered[(i - len(ordered)) % len(ordered)]
            variant = QUERY_VARIANTS_SINGLE[(i - len(ordered) + 1) % len(QUERY_VARIANTS_SINGLE)]
            note = "repeat_condition_due_to_only_14_condition_concepts"
        else:
            cond = ordered[i]
            variant = QUERY_VARIANTS_SINGLE[0]
            note = ""
        qid = f"F{start_idx + len(rows):03d}"
        cslot = gold_condition_slot(cond, gold_source=gold_source)
        rows.append(
            QueryRow(
                id=qid,
                query=variant.format(condition=cond.query_name),
                tier="A",
                level="L1",
                pattern="Condition",
                condition_name=cond.query_name,
                condition_concept_id=cond.concept_id,
                codeset_ids=cslot,
                gold_slots=cslot,
                gold_source=gold_source,
                selection_count_basis=cond.patient_count,
                notes=note,
            )
        )
        i += 1
    return rows, start_idx + len(rows)


def choose_l2(
    conditions_by_id: Mapping[int, ConditionInfo],
    demo_rows: Sequence[Mapping[str, Any]],
    target: int,
    min_count: int,
    gold_source: str,
    start_idx: int,
) -> Tuple[List[QueryRow], int]:
    candidates: List[Dict[str, Any]] = []
    for r in demo_rows:
        cid = int(r["condition_concept_id"])
        cond = conditions_by_id.get(cid)
        if not cond:
            continue
        specs = [
            ("female", FEMALE_CONCEPT_ID, None, "female", r.get("female")),
            ("male", MALE_CONCEPT_ID, None, "male", r.get("male")),
            ("", None, 40, "age_ge_40", r.get("age_ge_40")),
            ("", None, 50, "age_ge_50", r.get("age_ge_50")),
            ("", None, 60, "age_ge_60", r.get("age_ge_60")),
            ("", None, 65, "age_ge_65", r.get("age_ge_65")),
            ("female", FEMALE_CONCEPT_ID, 40, "female_age_ge_40", r.get("female_age_ge_40")),
            ("female", FEMALE_CONCEPT_ID, 50, "female_age_ge_50", r.get("female_age_ge_50")),
            ("female", FEMALE_CONCEPT_ID, 60, "female_age_ge_60", r.get("female_age_ge_60")),
            ("female", FEMALE_CONCEPT_ID, 65, "female_age_ge_65", r.get("female_age_ge_65")),
            ("male", MALE_CONCEPT_ID, 40, "male_age_ge_40", r.get("male_age_ge_40")),
            ("male", MALE_CONCEPT_ID, 50, "male_age_ge_50", r.get("male_age_ge_50")),
            ("male", MALE_CONCEPT_ID, 60, "male_age_ge_60", r.get("male_age_ge_60")),
            ("male", MALE_CONCEPT_ID, 65, "male_age_ge_65", r.get("male_age_ge_65")),
        ]
        for gender, gender_id, age_min, label, count in specs:
            n = int(count or 0)
            if n >= min_count:
                candidates.append(
                    {
                        "cond": cond,
                        "gender": gender,
                        "gender_id": gender_id,
                        "age_min": age_min,
                        "count": n,
                        "label": label,
                    }
                )
    # Prefer diversity first, then larger strata.  The modulo score avoids taking
    # all variants from hyperlipidemia before lower-prevalence conditions.
    candidates.sort(key=lambda x: (x["cond"].canonical_name, -x["count"], str(x["label"])))
    picked: List[Dict[str, Any]] = []
    used_keys: set[Tuple[str, str, Optional[int]]] = set()
    # Round-robin by condition.
    by_cond: Dict[str, List[Dict[str, Any]]] = {}
    for c in candidates:
        by_cond.setdefault(c["cond"].canonical_name, []).append(c)
    while len(picked) < target and any(by_cond.values()):
        for cname in sorted(list(by_cond.keys())):
            if len(picked) >= target:
                break
            bucket = by_cond[cname]
            while bucket:
                cand = bucket.pop(0)
                key = (cand["cond"].canonical_name, cand["gender"] or "any", cand["age_min"])
                if key in used_keys:
                    continue
                used_keys.add(key)
                picked.append(cand)
                break
    rows: List[QueryRow] = []
    for cand in picked:
        cond: ConditionInfo = cand["cond"]
        gender = str(cand["gender"] or "")
        age_min = cand["age_min"]
        if gender and age_min:
            q = f"How many {gender} patients aged {age_min} or older with {cond.query_name} are in this database?"
            pattern = "Condition + age + gender"
        elif gender:
            q = f"How many {gender} patients with {cond.query_name} are in this database?"
            pattern = "Condition + gender"
        else:
            q = f"How many patients aged {age_min} or older with {cond.query_name} are in this database?"
            pattern = "Condition + age"
        qid = f"F{start_idx + len(rows):03d}"
        cslot = gold_condition_slot(cond, gold_source=gold_source)
        rows.append(
            QueryRow(
                id=qid,
                query=q,
                tier="A",
                level="L2",
                pattern=pattern,
                condition_name=cond.query_name,
                condition_concept_id=cond.concept_id,
                age_min=age_min,
                gender=gender,
                gender_concept_id=cand["gender_id"],
                codeset_ids=cslot,
                gold_slots=cslot,
                gold_source=gold_source,
                selection_count_basis=int(cand["count"]),
            )
        )
    return rows, start_idx + len(rows)


def choose_l3(
    conditions_by_id: Mapping[int, ConditionInfo],
    year_rows: Sequence[Mapping[str, Any]],
    target: int,
    gold_source: str,
    start_idx: int,
) -> Tuple[List[QueryRow], int]:
    candidates: List[Dict[str, Any]] = []
    for r in year_rows:
        cond = conditions_by_id.get(int(r["condition_concept_id"]))
        if not cond:
            continue
        year = int(r["dx_year"])
        n = int(r.get("patient_count") or 0)
        if year in (2023, 2024) and n > 0:
            candidates.append({"cond": cond, "year": year, "count": n})
    # Alternate years and conditions for balance.
    candidates.sort(key=lambda x: (x["year"], -x["count"], x["cond"].canonical_name))
    picked: List[Dict[str, Any]] = []
    used: set[Tuple[str, int]] = set()
    for year in (2023, 2024):
        for cand in [c for c in candidates if c["year"] == year]:
            if len(picked) >= target:
                break
            key = (cand["cond"].canonical_name, year)
            if key in used:
                continue
            used.add(key)
            picked.append(cand)
        if len(picked) >= target:
            break
    # If target > one-year pass, fill remaining from all candidates.
    for cand in candidates:
        if len(picked) >= target:
            break
        key = (cand["cond"].canonical_name, cand["year"])
        if key not in used:
            used.add(key)
            picked.append(cand)
    rows: List[QueryRow] = []
    for cand in picked[:target]:
        cond: ConditionInfo = cand["cond"]
        year = int(cand["year"])
        qid = f"F{start_idx + len(rows):03d}"
        cslot = gold_condition_slot(cond, gold_source=gold_source)
        rows.append(
            QueryRow(
                id=qid,
                query=f"How many patients were newly diagnosed with {cond.query_name} in {year}?",
                tier="A",
                level="L3",
                pattern="Condition + year",
                condition_name=cond.query_name,
                condition_concept_id=cond.concept_id,
                year=year,
                requires_temporal=True,
                codeset_ids=cslot,
                gold_slots=cslot,
                gold_source=gold_source,
                selection_count_basis=int(cand["count"]),
            )
        )
    return rows, start_idx + len(rows)


def choose_l4(
    conditions_by_id: Mapping[int, ConditionInfo],
    drugs_by_id: Mapping[int, DrugInfo],
    pair_rows: Sequence[Mapping[str, Any]],
    target: int,
    min_count: int,
    gold_source: str,
    start_idx: int,
) -> Tuple[List[QueryRow], int]:
    candidates: List[Dict[str, Any]] = []
    for r in pair_rows:
        cond = conditions_by_id.get(int(r["condition_concept_id"]))
        drug = drugs_by_id.get(int(r["drug_concept_id"]))
        if not cond or not drug:
            continue
        pref = {norm_text(x) for x in L4_PREFERRED_DRUGS.get(cond.canonical_name, ())}
        is_preferred = norm_text(drug.canonical_name) in pref or norm_text(drug.query_name) in pref
        for window, col in [(90, "n_90d"), (365, "n_365d")]:
            n = int(r.get(col) or 0)
            if n >= min_count:
                candidates.append(
                    {
                        "cond": cond,
                        "drug": drug,
                        "window": window,
                        "count": n,
                        "preferred": is_preferred,
                    }
                )
    # Preferred clinical mappings first, then fill with actual high-count pairs.
    candidates.sort(key=lambda x: (not x["preferred"], x["cond"].canonical_name, x["window"], -x["count"], x["drug"].canonical_name))
    picked: List[Dict[str, Any]] = []
    used: set[Tuple[str, str, int]] = set()
    by_cond: Dict[str, List[Dict[str, Any]]] = {}
    for c in candidates:
        by_cond.setdefault(c["cond"].canonical_name, []).append(c)
    while len(picked) < target and any(by_cond.values()):
        for cname in sorted(list(by_cond.keys())):
            if len(picked) >= target:
                break
            bucket = by_cond[cname]
            while bucket:
                cand = bucket.pop(0)
                key = (cand["cond"].canonical_name, cand["drug"].canonical_name, int(cand["window"]))
                if key in used:
                    continue
                used.add(key)
                picked.append(cand)
                break
    rows: List[QueryRow] = []
    for cand in picked:
        cond: ConditionInfo = cand["cond"]
        drug: DrugInfo = cand["drug"]
        window = int(cand["window"])
        window_text = "1 year" if window == 365 else f"{window} days"
        qid = f"F{start_idx + len(rows):03d}"
        cslot = gold_condition_slot(cond, gold_source=gold_source)
        dslot = gold_concept_slot([drug.concept_id], gold_source=gold_source)
        rows.append(
            QueryRow(
                id=qid,
                query=f"How many patients with {cond.query_name} were prescribed {drug.query_name} within {window_text} after diagnosis?",
                tier="A",
                level="L4",
                pattern="Condition AFTER_DX Drug",
                condition_name=cond.query_name,
                condition_concept_id=cond.concept_id,
                drug_name=drug.query_name,
                drug_concept_id=drug.concept_id,
                window_days=window,
                requires_temporal=True,
                gold_temporal_direction="after",
                gold_temporal_window_days=window,
                codeset_ids=f"{cslot} | {dslot}",
                gold_slots=f"{cslot} | {dslot}",
                gold_source=gold_source,
                selection_count_basis=int(cand["count"]),
                notes="preferred_clinical_pair" if cand["preferred"] else "actual_pair_fallback",
            )
        )
    return rows, start_idx + len(rows)


def choose_l5(
    conditions_by_id: Mapping[int, ConditionInfo],
    l5_rows: Sequence[Mapping[str, Any]],
    target: int,
    min_count: int,
    gold_source: str,
    start_idx: int,
) -> Tuple[List[QueryRow], int]:
    candidates: List[Dict[str, Any]] = []
    for r in l5_rows:
        cond = conditions_by_id.get(int(r["condition_concept_id"]))
        if not cond:
            continue
        class_name = str(r["drug_class"])
        preferred = class_name in set(L5_PREFERRED_CLASSES.get(cond.canonical_name, ()))
        n = int(r.get("without_drug_after_dx") or 0)
        if n >= min_count:
            candidates.append({"cond": cond, "class_name": class_name, "ids": str(r["drug_concept_ids"]), "count": n, "preferred": preferred})
    candidates.sort(key=lambda x: (not x["preferred"], x["cond"].canonical_name, -x["count"], x["class_name"]))
    picked: List[Dict[str, Any]] = []
    used: set[Tuple[str, str]] = set()
    by_cond: Dict[str, List[Dict[str, Any]]] = {}
    for c in candidates:
        by_cond.setdefault(c["cond"].canonical_name, []).append(c)
    while len(picked) < target and any(by_cond.values()):
        for cname in sorted(list(by_cond.keys())):
            if len(picked) >= target:
                break
            bucket = by_cond[cname]
            while bucket:
                cand = bucket.pop(0)
                key = (cand["cond"].canonical_name, cand["class_name"])
                if key in used:
                    continue
                used.add(key)
                picked.append(cand)
                break
    rows: List[QueryRow] = []
    for cand in picked:
        cond: ConditionInfo = cand["cond"]
        ids = [int(x) for x in re.split(r"\s*,\s*", cand["ids"]) if x.strip()]
        cslot = gold_condition_slot(cond, gold_source=gold_source)
        dslot = gold_concept_slot(ids, gold_source=gold_source)
        qid = f"F{start_idx + len(rows):03d}"
        rows.append(
            QueryRow(
                id=qid,
                query=f"How many patients with {cond.query_name} had no prescription of any {cand['class_name']} after diagnosis?",
                tier="A",
                level="L5",
                pattern="Condition AFTER_DX NOT DrugClass",
                condition_name=cond.query_name,
                condition_concept_id=cond.concept_id,
                drug_class=str(cand["class_name"]),
                drug_concept_ids=",".join(str(x) for x in ids),
                requires_temporal=True,
                gold_temporal_direction="after",
                codeset_ids=f"{cslot} | {dslot}",
                gold_slots=f"{cslot} | {dslot}",
                gold_source=gold_source,
                selection_count_basis=int(cand["count"]),
                notes="preferred_not_class" if cand["preferred"] else "actual_not_fallback",
            )
        )
    return rows, start_idx + len(rows)


def verify_negative_controls_absent(db: Any, schema: str, vocab_schema: str) -> Dict[str, bool]:
    """Return {category: absent_from_resident_condition_or_procedure_inventory}.

    For each Tier-B category, all configured disease/procedure aliases must be
    absent from resident condition_occurrence and procedure_occurrence concept
    names. This prevents accidentally labeling a populated concept as a negative
    control.
    """
    out: Dict[str, bool] = {}
    for category, aliases in NEGATIVE_CONTROL_ABSENCE_TERMS.items():
        found_any = False
        for term in aliases:
            pattern = norm_text(term).replace(" ", "%")
            rows = fetch_json_rows(
                db,
                f"""
                WITH resident AS (
                    SELECT
                        'condition'::text AS resident_domain,
                        co.condition_concept_id::bigint AS concept_id,
                        c.concept_name,
                        COUNT(DISTINCT co.person_id)::bigint AS n_patients
                    FROM {schema}.condition_occurrence co
                    JOIN {vocab_schema}.concept c
                      ON c.concept_id = co.condition_concept_id
                    WHERE LOWER(c.concept_name) LIKE '%{pattern}%'
                    GROUP BY co.condition_concept_id, c.concept_name

                    UNION ALL

                    SELECT
                        'procedure'::text AS resident_domain,
                        po.procedure_concept_id::bigint AS concept_id,
                        c.concept_name,
                        COUNT(DISTINCT po.person_id)::bigint AS n_patients
                    FROM {schema}.procedure_occurrence po
                    JOIN {vocab_schema}.concept c
                      ON c.concept_id = po.procedure_concept_id
                    WHERE LOWER(c.concept_name) LIKE '%{pattern}%'
                    GROUP BY po.procedure_concept_id, c.concept_name
                )
                SELECT *
                FROM resident
                LIMIT 5
                """,
            )
            if rows:
                found_any = True
                break
        out[category] = not found_any
    return out


def choose_tier_b(
    db: Any,
    schema: str,
    vocab_schema: str,
    target: int,
    start_idx: int,
    gold_source: str,
) -> Tuple[List[QueryRow], int]:
    """Emit Tier-B negative controls with empty gold slots and zero gold count.

    Tier B is not sampled from resident concepts by definition. It is included
    to test whether systems hallucinate nonzero cohorts for clinically
    meaningful but data-absent requests.
    """
    if target <= 0:
        return [], start_idx
    if target > len(NEGATIVE_CONTROL_QUERIES):
        raise SystemExit(
            f"Requested {target} Tier-B queries, but only {len(NEGATIVE_CONTROL_QUERIES)} "
            "negative-control templates are defined."
        )
    absent = verify_negative_controls_absent(db, schema, vocab_schema)
    rows: List[QueryRow] = []
    for item in NEGATIVE_CONTROL_QUERIES[:target]:
        category = str(item["category"])
        if not absent.get(category, False):
            raise SystemExit(
                f"Tier-B negative-control category is not absent from the resident inventory: {category!r}. "
                "Remove it from NEGATIVE_CONTROL_QUERIES or replace the category."
            )
        qid = f"F{start_idx + len(rows):03d}"
        query_text = str(item["query"])
        rows.append(
            QueryRow(
                id=qid,
                query=query_text,
                tier="B",
                level="TierB",
                pattern="NegativeControl",
                condition_name=category,
                year=None,
                requires_temporal=False,
                requires_numeric=False,
                codeset_ids="",
                gold_slots="",
                gold_source=gold_source,
                selection_count_basis=0,
                notes=f"negative_control_zero_by_construction;category={category}",
            )
        )
    return rows, start_idx + len(rows)


def enforce_counts(rows: Sequence[QueryRow], targets: Mapping[str, int]) -> None:
    counts: Dict[str, int] = {}
    for row in rows:
        counts[row.level] = counts.get(row.level, 0) + 1
    problems = []
    for level, target in targets.items():
        got = counts.get(level, 0)
        if got != target:
            problems.append(f"{level}: expected {target}, got {got}")
    if problems:
        raise SystemExit("Could not generate requested benchmark shape: " + "; ".join(problems))


def main() -> int:
    p = argparse.ArgumentParser(description="Build a CDM-resident OMOP executable-reference benchmark")
    p.add_argument("--out-dir", type=Path, default=Path("outputs/omop_executable_benchmark"))
    p.add_argument("--schema", default=DEFAULT_SCHEMA)
    p.add_argument("--vocab-schema", default=DEFAULT_VOCAB_SCHEMA)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                   help="Dataset-specific JSON config for aliases, preferred pairs, and negative controls")
    p.add_argument("--gold-source", choices=["athena", "omop_direct"], default="athena",
                   help="Gold concept-set source. The default athena mode uses resident OMOP/Athena seed concepts.")
    p.add_argument("--allow-l1-repeat", action="store_true", default=True, help="Allow one repeated single-condition query if only 14 conditions are present")
    p.add_argument("--no-allow-l1-repeat", dest="allow_l1_repeat", action="store_false")
    p.add_argument("--target-l1", type=int, default=12)
    p.add_argument("--target-l2", type=int, default=18)
    p.add_argument("--target-l3", type=int, default=14)
    p.add_argument("--target-l4", type=int, default=22)
    p.add_argument("--target-l5", type=int, default=14)
    p.add_argument("--target-tier-b", type=int, default=20, help="Number of Tier-B negative controls")
    p.add_argument("--no-tier-b", dest="include_tier_b", action="store_false", default=True, help="Disable Tier-B rows and generate a positive-only set.")
    p.add_argument("--min-l2-count", type=int, default=50)
    p.add_argument("--min-l4-count", type=int, default=200)
    p.add_argument("--min-l5-count", type=int, default=1)
    p.add_argument("--jsonl-name", default="omop_benchmark_queries.jsonl")
    p.add_argument("--mapping-name", default="omop_benchmark_mapping.csv")
    args = p.parse_args()
    load_benchmark_config(args.config)

    targets = {
        "L1": args.target_l1,
        "L2": args.target_l2,
        "L3": args.target_l3,
        "L4": args.target_l4,
        "L5": args.target_l5,
    }
    positive_n = sum(targets.values())
    tier_b_n = int(args.target_tier_b) if args.include_tier_b else 0
    total_n = positive_n + tier_b_n
    if total_n != 100:
        raise SystemExit(
            f"The default benchmark shape must contain 100 queries. Got Tier-A L1-L5={positive_n} "
            f"plus Tier-B={tier_b_n} => {total_n}. Targets={targets}"
        )

    db = make_db()
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    print("[OMOP benchmark] collecting capability matrix from the target OMOP CDM...")
    condition_raw = collect_condition_inventory(db, args.schema, args.vocab_schema)
    drug_raw = collect_drug_inventory(db, args.schema, args.vocab_schema)
    demo_rows = collect_demographic_capability(db, args.schema, args.vocab_schema)
    year_rows = collect_year_capability(db, args.schema, args.vocab_schema)
    pair_rows = collect_condition_to_drug(db, args.schema, args.vocab_schema)

    conditions = materialize_conditions(condition_raw)
    drugs = materialize_drugs(drug_raw)
    conditions_by_id = {c.concept_id: c for c in conditions}
    drugs_by_id = {d.concept_id: d for d in drugs}
    drug_classes = resolve_drug_class(drugs)
    l5_rows = collect_l5_not_capability(db, args.schema, conditions, drug_classes)

    # Persist the capability matrix before query sampling so that every emitted
    # row can be audited against an actual DB count.
    cond_tsv_rows = [{**r, "canonical_name": canonical_condition_name(str(r.get("concept_name", "")))} for r in condition_raw]
    drug_tsv_rows = [{**r, "canonical_name": canonical_drug_name(str(r.get("concept_name", "")))} for r in drug_raw]
    write_tsv(out_dir / "resident_condition_inventory.tsv", cond_tsv_rows)
    write_tsv(out_dir / "resident_drug_inventory.tsv", drug_tsv_rows)
    write_tsv(out_dir / "resident_condition_demographic.tsv", demo_rows)
    write_tsv(out_dir / "resident_year_distribution.tsv", year_rows)
    write_tsv(out_dir / "resident_condition_to_drug_after_dx.tsv", pair_rows)
    class_rows = []
    for cls, members in drug_classes.items():
        class_rows.append(
            {
                "drug_class": cls,
                "n_present_ingredients": len(members),
                "drug_concept_ids": ",".join(str(d.concept_id) for d in members),
                "drug_names": "; ".join(d.query_name for d in members),
            }
        )
    write_tsv(out_dir / "resident_drug_class_inventory.tsv", class_rows)
    write_tsv(out_dir / "resident_l5_not_drug_capability.tsv", l5_rows)

    print("[OMOP benchmark] sampling query strata from capability matrix...")
    all_rows: List[QueryRow] = []
    next_idx = 1
    part, next_idx = choose_l1(conditions, args.target_l1, args.gold_source, args.allow_l1_repeat, next_idx)
    all_rows.extend(part)
    part, next_idx = choose_l2(conditions_by_id, demo_rows, args.target_l2, args.min_l2_count, args.gold_source, next_idx)
    all_rows.extend(part)
    part, next_idx = choose_l3(conditions_by_id, year_rows, args.target_l3, args.gold_source, next_idx)
    all_rows.extend(part)
    part, next_idx = choose_l4(conditions_by_id, drugs_by_id, pair_rows, args.target_l4, args.min_l4_count, args.gold_source, next_idx)
    all_rows.extend(part)
    part, next_idx = choose_l5(conditions_by_id, l5_rows, args.target_l5, args.min_l5_count, args.gold_source, next_idx)
    all_rows.extend(part)
    if args.include_tier_b and args.target_tier_b > 0:
        part, next_idx = choose_tier_b(db, args.schema, args.vocab_schema, args.target_tier_b, next_idx, args.gold_source)
        all_rows.extend(part)

    enforce_counts([r for r in all_rows if r.tier == "A"], targets)

    # Re-number in case any selection changes are introduced later.
    for i, row in enumerate(all_rows, 1):
        row.id = f"F{i:03d}"

    jsonl_path = out_dir / args.jsonl_name
    with jsonl_path.open("w", encoding="utf-8") as f:
        for row in all_rows:
            f.write(json.dumps(row.to_json_obj(), ensure_ascii=False) + "\n")

    mapping_path = out_dir / args.mapping_name
    fieldnames = list(QueryRow.__dataclass_fields__.keys())
    with mapping_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in all_rows:
            w.writerow(asdict(row))

    summary = {
        "dataset_version": DATASET_VERSION,
        "schema": args.schema,
        "vocab_schema": args.vocab_schema,
        "config": str(args.config),
        "n_queries": len(all_rows),
        "targets": {**targets, "TierB": tier_b_n},
        "counts_by_level": {level: sum(1 for r in all_rows if r.level == level) for level in sorted({r.level for r in all_rows})},
        "counts_by_tier": {tier: sum(1 for r in all_rows if r.tier == tier) for tier in sorted({r.tier for r in all_rows})},
        "n_conditions_in_db": len(conditions),
        "n_drugs_in_db": len(drugs),
        "n_drug_classes_populated": len(drug_classes),
        "n_condition_to_drug_pairs": len(pair_rows),
        "min_l2_count": args.min_l2_count,
        "min_l4_count": args.min_l4_count,
        "min_l5_count": args.min_l5_count,
        "gold_source": args.gold_source,
        "jsonl": str(jsonl_path),
        "mapping_csv": str(mapping_path),
    }
    (out_dir / "omop_capability_matrix.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n[OMOP benchmark] done")
    print(f"  JSONL:       {jsonl_path}")
    print(f"  Mapping CSV: {mapping_path}")
    print(f"  Capability:  {out_dir}")
    print(f"  Counts:      {summary['counts_by_level']} | tiers={summary['counts_by_tier']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
