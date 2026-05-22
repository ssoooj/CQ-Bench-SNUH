"""Compute executable reference counts for a CDM-resident OMOP benchmark.

Gold rule
---------
Every condition/drug slot is interpreted as an OMOP/Athena seed concept:

    seed concept(s) -> concept_ancestor descendant expansion -> standard valid concepts
    -> intersect with concepts actually resident in the target clinical tables.

This script produces executable-reference counts for benchmark-defined cohort
semantics. It does not produce chart-reviewed clinical labels.

Supported slot syntax in mapping CSV:
    omop:432867
    athena:432867
    omop:432867,316866
    432867                 # treated as an OMOP seed concept ID

Usage:
    python derive_omop_reference_counts.py \
      --mapping-csv outputs/omop_executable_benchmark/omop_benchmark_mapping.csv \
      --jsonl outputs/omop_executable_benchmark/omop_benchmark_queries.jsonl \
      --output outputs/omop_executable_benchmark/omop_reference_counts.json
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

DATASET_VERSION = "omop_reference_counts_resident_tierb_2023_2024"
DEFAULT_SCHEMA = "synthetic_snuh_cdm"
DEFAULT_VOCAB_SCHEMA = "public"
MALE_CONCEPT_ID = 8507
FEMALE_CONCEPT_ID = 8532

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@dataclass
class GoldEntry:
    id: str
    tier: str
    level: str
    pattern: str
    query: str
    gold_standard: str
    gold_slots: List[str]
    slot_roles: List[str]
    seed_concept_ids_by_slot: List[List[int]]
    slot_concept_ids: List[List[int]]
    gold_concept_ids: List[int]
    gold_concept_count: int
    gold_cohort_count: Optional[int]
    gold_sql: Optional[str]
    template_used: Optional[str]
    status: str
    stage: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    notes: str = ""


def make_db() -> Any:
    try:
        from config_loader import ConfigLoader  # type: ignore
        from db_connector import DBConnector  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "Could not import ConfigLoader/DBConnector. Run this script from the project "
            "repository where settings.yaml and db_connector.py exist."
        ) from exc
    return DBConnector(ConfigLoader())


def strip_sql_semicolon(sql: str) -> str:
    return sql.strip().rstrip(";")


def fetch_json_rows(db: Any, sql: str) -> List[Dict[str, Any]]:
    wrapped = (
        "SELECT COALESCE(jsonb_agg(to_jsonb(q)), '[]'::jsonb)::text "
        f"FROM ({strip_sql_semicolon(sql)}) q"
    )
    raw = db.fetch_scalar(wrapped)
    if raw is None:
        return []
    if isinstance(raw, str):
        return json.loads(raw)
    try:
        return [dict(x) for x in raw]
    except TypeError as exc:
        raise TypeError(f"Unexpected JSON payload type from fetch_scalar: {type(raw)!r}") from exc


def load_mapping(path: Path) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({str(k): (v or "").strip() for k, v in row.items()})
    return rows


def load_query_jsonl(path: Path) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        obj = json.loads(line)
        if "id" not in obj or "query" not in obj:
            raise SystemExit(f"{path}:{line_no}: JSONL row must contain id and query")
        out[str(obj["id"]).upper()] = obj
    return out


def split_slots(s: str) -> List[str]:
    if not s:
        return []
    return [p.strip() for p in re.split(r"\s*\|\s*", s) if p.strip()]


def ints_from_text(s: str) -> List[int]:
    vals: List[int] = []
    for m in re.finditer(r"\d+", s or ""):
        try:
            vals.append(int(m.group(0)))
        except ValueError:
            pass
    return vals


def parse_int_field(row: Dict[str, str], key: str) -> Optional[int]:
    val = row.get(key)
    if val is None or str(val).strip() == "":
        return None
    try:
        return int(float(str(val).strip()))
    except ValueError:
        return None


def fallback_gold_slots_from_row(row: Dict[str, str]) -> List[str]:
    slots: List[str] = []
    cid = parse_int_field(row, "condition_concept_id")
    if cid is not None:
        slots.append(f"omop:{cid}")
    did = parse_int_field(row, "drug_concept_id")
    if did is not None:
        slots.append(f"omop:{did}")
    elif row.get("drug_concept_ids"):
        ids = ",".join(str(x) for x in ints_from_text(row["drug_concept_ids"]))
        if ids:
            slots.append(f"omop:{ids}")
    return slots


def slot_seed_ids(slot: str) -> List[int]:
    raw = slot.strip()
    if not raw:
        return []
    lower = raw.lower()
    if lower.startswith(("omop:", "athena:", "concept:", "direct:")):
        raw = raw.split(":", 1)[1]
    # Bare numbers are interpreted as OMOP concept IDs.
    return sorted(set(ints_from_text(raw)))


def infer_slot_role(row: Dict[str, str], idx: int, n_slots: int) -> str:
    pattern = (row.get("pattern") or "").lower()
    level = (row.get("level") or "").upper()
    if idx == 0:
        return "condition"
    if "drug" in pattern or level in {"L4", "L5"} or row.get("drug_concept_id") or row.get("drug_concept_ids"):
        return "drug"
    # Safe default for the current benchmark; extend when new slot roles are added.
    return "drug"


def sql_values_int(ids: Iterable[int]) -> str:
    clean = sorted({int(x) for x in ids})
    if not clean:
        return "(NULL)"
    return ",".join(f"({x})" for x in clean)


def sql_id_list(ids: Iterable[int]) -> str:
    clean = sorted({int(x) for x in ids})
    if not clean:
        return "(NULL)"
    return "(" + ",".join(str(x) for x in clean) + ")"


def resolve_athena_resident_slot(
    db: Any,
    seed_ids: Sequence[int],
    *,
    role: str,
    schema: str,
    vocab_schema: str,
    include_descendants: bool,
    resident_only: bool,
) -> Tuple[Set[int], str]:
    """Resolve OMOP seeds to executable resident standard concepts."""
    seeds = sorted({int(x) for x in seed_ids if int(x) > 0})
    if not seeds:
        return set(), "empty_seed"

    if role == "condition":
        domain = "Condition"
        resident_table = f"{schema}.condition_occurrence"
        resident_col = "condition_concept_id"
    elif role == "drug":
        domain = "Drug"
        resident_table = f"{schema}.drug_exposure"
        resident_col = "drug_concept_id"
    elif role == "measurement":
        domain = "Measurement"
        resident_table = f"{schema}.measurement"
        resident_col = "measurement_concept_id"
    elif role == "procedure":
        domain = "Procedure"
        resident_table = f"{schema}.procedure_occurrence"
        resident_col = "procedure_concept_id"
    else:
        raise ValueError(f"Unsupported slot role: {role!r}")

    expansion_sql = f"""
    WITH seed(concept_id) AS (
        VALUES {sql_values_int(seeds)}
    ),
    expanded AS (
        SELECT concept_id FROM seed
        {f'''UNION
        SELECT ca.descendant_concept_id AS concept_id
        FROM {vocab_schema}.concept_ancestor ca
        JOIN seed s ON s.concept_id = ca.ancestor_concept_id''' if include_descendants else ''}
    ),
    valid AS (
        SELECT DISTINCT e.concept_id::bigint AS concept_id
        FROM expanded e
        JOIN {vocab_schema}.concept c
          ON c.concept_id = e.concept_id
        WHERE c.domain_id = '{domain}'
          AND c.standard_concept = 'S'
          AND c.invalid_reason IS NULL
    ),
    resident AS (
        SELECT DISTINCT {resident_col}::bigint AS concept_id
        FROM {resident_table}
        WHERE {resident_col} <> 0
    )
    SELECT v.concept_id
    FROM valid v
    {('JOIN resident r ON r.concept_id = v.concept_id') if resident_only else ''}
    ORDER BY v.concept_id
    """
    rows = fetch_json_rows(db, expansion_sql)
    ids = {int(r["concept_id"]) for r in rows}
    note = (
        f"athena_seed={len(seeds)};role={role};descendants={str(include_descendants).lower()};"
        f"resident_only={str(resident_only).lower()};resolved={len(ids)}"
    )
    return ids, note


_AGE_RE = re.compile(r"aged\s+(\d+)\s+or\s+older", re.IGNORECASE)
_GENDER_FEMALE_RE = re.compile(r"\bfemale\b", re.IGNORECASE)
_GENDER_MALE_RE = re.compile(r"\bmale\b(?!\s+or\s+female)", re.IGNORECASE)
_YEAR_RE = re.compile(r"\bin\s+(20\d{2})\b", re.IGNORECASE)
_WITHIN_RE = re.compile(r"within\s+(\d+)\s+(day|days|month|months|year|years)", re.IGNORECASE)


def infer_age_min(row: Dict[str, str], query: str) -> Optional[int]:
    direct = parse_int_field(row, "age_min")
    if direct is not None:
        return direct
    m = _AGE_RE.search(query)
    return int(m.group(1)) if m else None


def infer_gender_concept_id(row: Dict[str, str], query: str) -> Optional[int]:
    direct = parse_int_field(row, "gender_concept_id")
    if direct is not None:
        return direct
    g = str(row.get("gender") or "").lower().strip()
    if g == "female":
        return FEMALE_CONCEPT_ID
    if g == "male":
        return MALE_CONCEPT_ID
    if _GENDER_FEMALE_RE.search(query):
        return FEMALE_CONCEPT_ID
    if _GENDER_MALE_RE.search(query):
        return MALE_CONCEPT_ID
    return None


def infer_year(row: Dict[str, str], query: str) -> Optional[int]:
    direct = parse_int_field(row, "year")
    if direct is not None:
        return direct
    m = _YEAR_RE.search(query)
    return int(m.group(1)) if m else None


def infer_window_days(row: Dict[str, str], query: str) -> Optional[int]:
    direct = parse_int_field(row, "window_days") or parse_int_field(row, "gold_temporal_window_days")
    if direct is not None:
        return direct
    m = _WITHIN_RE.search(query)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("day"):
        return n
    if unit.startswith("month"):
        return n * 30
    if unit.startswith("year"):
        return n * 365
    return None


def build_dx_cte(schema: str, condition_ids: Set[int]) -> str:
    return f"""
    WITH dx AS (
        SELECT person_id, MIN(condition_start_date)::date AS dx_date
        FROM {schema}.condition_occurrence
        WHERE condition_concept_id IN {sql_id_list(condition_ids)}
        GROUP BY person_id
    )
    """.strip()


def build_person_join_and_clauses(row: Dict[str, str], query: str, schema: str) -> Tuple[str, List[str]]:
    joins = ""
    clauses: List[str] = []
    age_min = infer_age_min(row, query)
    gender_id = infer_gender_concept_id(row, query)
    if age_min is not None or gender_id is not None:
        joins = f"JOIN {schema}.person p ON p.person_id = dx.person_id"
        if age_min is not None:
            clauses.append(f"(EXTRACT(YEAR FROM dx.dx_date)::int - p.year_of_birth) >= {int(age_min)}")
        if gender_id is not None:
            clauses.append(f"p.gender_concept_id = {int(gender_id)}")
    return joins, clauses


def build_gold_sql(
    row: Dict[str, str],
    query: str,
    slot_sets: Sequence[Set[int]],
    schema: str,
) -> Tuple[Optional[str], Optional[str], str]:
    tier = (row.get("tier") or "A").strip().upper()
    level = (row.get("level") or "").strip().upper()
    pattern = (row.get("pattern") or "").strip()
    if tier == "B" or pattern.lower() in {"negativecontrol", "negative_control", "tier b"}:
        return "SELECT 0::bigint AS cohort_count;", "auto:tier_b_zero", "zero_by_construction"

    primary_ids: Set[int] = set(slot_sets[0]) if slot_sets else set()
    if not primary_ids:
        cid = parse_int_field(row, "condition_concept_id")
        if cid is not None:
            primary_ids = {cid}
    if not primary_ids:
        return None, None, "empty_primary_concept"

    # Use resolved resident OMOP concepts for secondary drug slots. Do not fall
    # back to raw mapping IDs for L4/L5: if a seed does not resolve to a valid
    # resident Drug-domain concept, the row must fail instead of silently using
    # an invalid concept in SQL.
    secondary_ids: Set[int] = set(slot_sets[1]) if len(slot_sets) >= 2 else set()

    dx_cte = build_dx_cte(schema, primary_ids)
    person_join, clauses = build_person_join_and_clauses(row, query, schema)
    year = infer_year(row, query)
    if year is not None and (level == "L3" or "year" in pattern.lower()):
        clauses.append(f"dx.dx_date >= DATE '{int(year)}-01-01'")
        clauses.append(f"dx.dx_date < DATE '{int(year) + 1}-01-01'")

    where = ""
    if clauses:
        where = "\nWHERE " + "\n  AND ".join(clauses)

    # L4 AFTER_DX drug exposure within a relative window.
    if level == "L4" or "AFTER_DX" in pattern or "after_dx" in pattern.lower():
        if "NOT" not in pattern.upper() and level != "L5":
            if not secondary_ids:
                return None, None, "missing_drug_concepts"
            window_days = infer_window_days(row, query)
            if window_days is None:
                return None, None, "missing_window_days"
            sql = f"""
            {dx_cte}
            SELECT COUNT(DISTINCT dx.person_id)::bigint AS cohort_count
            FROM dx
            {person_join}
            {where}
            {"AND" if where else "WHERE"} EXISTS (
                SELECT 1
                FROM {schema}.drug_exposure de
                WHERE de.person_id = dx.person_id
                  AND de.drug_concept_id IN {sql_id_list(secondary_ids)}
                  AND de.drug_exposure_start_date >= dx.dx_date
                  AND de.drug_exposure_start_date <= dx.dx_date + INTERVAL '{int(window_days)} days'
            );
            """
            return re.sub(r"\n\s+\n", "\n", sql).strip(), "auto:L4_condition_after_dx_drug_window", "stage4_auto_temporal"

    # L5 temporal NOT: no exposure to drug/drug-class after diagnosis.
    if level == "L5" or "NOT" in pattern.upper():
        if not secondary_ids:
            return None, None, "missing_exclusion_drug_concepts"
        window_days = infer_window_days(row, query)
        window_clause = ""
        if window_days is not None:
            window_clause = f"\n                  AND de.drug_exposure_start_date <= dx.dx_date + INTERVAL '{int(window_days)} days'"
        sql = f"""
        {dx_cte}
        SELECT COUNT(DISTINCT dx.person_id)::bigint AS cohort_count
        FROM dx
        {person_join}
        {where}
        {"AND" if where else "WHERE"} NOT EXISTS (
            SELECT 1
            FROM {schema}.drug_exposure de
            WHERE de.person_id = dx.person_id
              AND de.drug_concept_id IN {sql_id_list(secondary_ids)}
              AND de.drug_exposure_start_date >= dx.dx_date{window_clause}
        );
        """
        return re.sub(r"\n\s+\n", "\n", sql).strip(), "auto:L5_condition_after_dx_not_drug", "stage4_auto_temporal_not"

    if "Drug" in pattern and secondary_ids:
        sql = f"""
        {dx_cte}
        SELECT COUNT(DISTINCT dx.person_id)::bigint AS cohort_count
        FROM dx
        {person_join}
        {where}
        {"AND" if where else "WHERE"} EXISTS (
            SELECT 1
            FROM {schema}.drug_exposure de
            WHERE de.person_id = dx.person_id
              AND de.drug_concept_id IN {sql_id_list(secondary_ids)}
        );
        """
        return re.sub(r"\n\s+\n", "\n", sql).strip(), "auto:condition_drug_anytime", "stage4_auto"

    sql = f"""
    {dx_cte}
    SELECT COUNT(DISTINCT dx.person_id)::bigint AS cohort_count
    FROM dx
    {person_join}
    {where};
    """
    template = "auto:L3_condition_first_dx_year" if level == "L3" else "auto:condition_demographic"
    return re.sub(r"\n\s+\n", "\n", sql).strip(), template, "stage4_auto"


def execute_count_sql(db: Any, sql: str) -> Optional[int]:
    value = db.fetch_scalar(sql)
    if value is None:
        return None
    return int(value)


def required_slot_error(tier: str, level: str, pattern: str, slot_sets: List[Set[int]]) -> str:
    """Return a non-empty message when a non-Tier-B query has unresolved required slots."""
    if tier.upper() == "B" or pattern.lower() in {"negativecontrol", "negative_control", "tier b"}:
        return ""
    lens = [len(s) for s in slot_sets]
    if level in {"L1", "L2", "L3"}:
        if len(slot_sets) < 1 or len(slot_sets[0]) == 0:
            return f"{level} requires one resolved condition slot; slot_lengths={lens}"
    elif level == "L4":
        if len(slot_sets) < 2 or len(slot_sets[0]) == 0 or len(slot_sets[1]) == 0:
            return f"L4 requires resolved condition and drug slots; slot_lengths={lens}"
    elif level == "L5":
        if len(slot_sets) < 2 or len(slot_sets[0]) == 0 or len(slot_sets[1]) == 0:
            return f"L5 requires resolved condition and drug/drug-class slots; slot_lengths={lens}"
    return ""

def process_one(
    row: Dict[str, str],
    query_obj: Dict[str, Any],
    db: Any,
    *,
    schema: str,
    vocab_schema: str,
    include_descendants: bool,
    resident_only: bool,
    skip_cohort_count: bool,
) -> GoldEntry:
    qid = (row.get("id") or query_obj.get("id") or "").upper()
    query = str(row.get("query") or query_obj.get("query") or "").strip()
    tier = (row.get("tier") or str(query_obj.get("tier") or "A")).strip().upper() or "A"
    level = (row.get("level") or str(query_obj.get("level") or "")).strip()
    pattern = (row.get("pattern") or str(query_obj.get("pattern") or "")).strip()

    slot_spec = row.get("gold_slots") or row.get("codeset_ids") or ""
    slots = split_slots(slot_spec) or fallback_gold_slots_from_row(row)
    roles = [infer_slot_role(row, i, len(slots)) for i, _ in enumerate(slots)]
    seed_sets = [slot_seed_ids(slot) for slot in slots]

    slot_sets: List[Set[int]] = []
    notes: List[str] = []
    for slot, role, seeds in zip(slots, roles, seed_sets):
        ids, note = resolve_athena_resident_slot(
            db,
            seeds,
            role=role,
            schema=schema,
            vocab_schema=vocab_schema,
            include_descendants=include_descendants,
            resident_only=resident_only,
        )
        slot_sets.append(ids)
        notes.append(f"{slot}:{note}")

    union_ids: Set[int] = set()
    for s in slot_sets:
        union_ids |= s

    metadata: Dict[str, Any] = {}
    for k, v in {**query_obj, **row}.items():
        if k not in {"id", "query"} and v not in (None, ""):
            metadata[k] = v
    metadata["gold_source"] = "ATHENA_RESIDENT_OMOP"
    metadata["include_descendants"] = include_descendants
    metadata["resident_only"] = resident_only

    if tier != "B" and pattern.lower() not in {"negativecontrol", "negative_control", "tier b"}:
        slot_err = required_slot_error(tier, level, pattern, slot_sets)
        if slot_err:
            return GoldEntry(
                id=qid,
                tier=tier,
                level=level,
                pattern=pattern,
                query=query,
                gold_standard="ATHENA_RESIDENT_OMOP",
                gold_slots=slots,
                slot_roles=roles,
                seed_concept_ids_by_slot=seed_sets,
                slot_concept_ids=[sorted(s) for s in slot_sets],
                gold_concept_ids=sorted(union_ids),
                gold_concept_count=len(union_ids),
                gold_cohort_count=None,
                gold_sql=None,
                template_used=None,
                status="error",
                stage="slot_validation",
                metadata=metadata,
                notes=("; ".join(notes) + "; " if notes else "") + slot_err,
            )

    if tier == "B" or pattern.lower() in {"negativecontrol", "negative_control", "tier b"}:
        sql, template, stage = build_gold_sql(row, query, slot_sets, schema)
        return GoldEntry(
            id=qid,
            tier=tier,
            level=level,
            pattern=pattern,
            query=query,
            gold_standard="ATHENA_RESIDENT_OMOP",
            gold_slots=slots,
            slot_roles=roles,
            seed_concept_ids_by_slot=seed_sets,
            slot_concept_ids=[sorted(s) for s in slot_sets],
            gold_concept_ids=[],
            gold_concept_count=0,
            gold_cohort_count=0,
            gold_sql=sql,
            template_used=template,
            status="ok" if not skip_cohort_count else "concepts_only",
            stage=stage,
            metadata=metadata,
            notes="zero_by_construction",
        )

    entry = GoldEntry(
        id=qid,
        tier=tier,
        level=level,
        pattern=pattern,
        query=query,
        gold_standard="ATHENA_RESIDENT_OMOP",
        gold_slots=slots,
        slot_roles=roles,
        seed_concept_ids_by_slot=seed_sets,
        slot_concept_ids=[sorted(s) for s in slot_sets],
        gold_concept_ids=sorted(union_ids),
        gold_concept_count=len(union_ids),
        gold_cohort_count=None,
        gold_sql=None,
        template_used=None,
        status="empty_gold" if not union_ids else "ok",
        stage="",
        metadata=metadata,
        notes="; ".join(notes),
    )

    if not union_ids:
        return entry
    if skip_cohort_count:
        entry.status = "concepts_only"
        entry.stage = "concept_ids_only"
        return entry

    sql, template, stage = build_gold_sql(row, query, slot_sets, schema)
    entry.gold_sql = sql
    entry.template_used = template
    entry.stage = stage
    if not sql:
        entry.status = "manual_required"
        entry.notes = (entry.notes + "; " if entry.notes else "") + f"sql_template_failed:{stage}"
        return entry

    try:
        entry.gold_cohort_count = execute_count_sql(db, sql)
        entry.status = "ok"
    except Exception as exc:  # pragma: no cover
        entry.status = "error"
        entry.notes = (entry.notes + "; " if entry.notes else "") + f"exec_error:{type(exc).__name__}:{str(exc)[:300]}"
    return entry


def main() -> int:
    p = argparse.ArgumentParser(description="Derive OMOP-resident executable reference counts")
    p.add_argument("--mapping-csv", type=Path, default=Path("outputs/omop_executable_benchmark/omop_benchmark_mapping.csv"))
    p.add_argument("--jsonl", type=Path, default=Path("outputs/omop_executable_benchmark/omop_benchmark_queries.jsonl"))
    p.add_argument("--output", type=Path, default=Path("outputs/omop_executable_benchmark/omop_reference_counts.json"))
    p.add_argument("--schema", default=DEFAULT_SCHEMA)
    p.add_argument("--vocab-schema", default=DEFAULT_VOCAB_SCHEMA)
    p.add_argument("--only", type=str, default="")
    p.add_argument("--skip-cohort-count", action="store_true")
    p.add_argument("--no-descendants", dest="include_descendants", action="store_false", default=True)
    p.add_argument("--no-resident-only", dest="resident_only", action="store_false", default=True)
    args = p.parse_args()

    if not args.mapping_csv.is_file():
        print(f"error: mapping CSV not found: {args.mapping_csv}", file=sys.stderr)
        return 2
    if not args.jsonl.is_file():
        print(f"error: JSONL not found: {args.jsonl}", file=sys.stderr)
        return 2

    mapping = load_mapping(args.mapping_csv)
    queries = load_query_jsonl(args.jsonl)
    only_set = {x.strip().upper() for x in args.only.split(",") if x.strip()} if args.only else None

    db = make_db()
    print(
        f"[Gold-OMOP-resident] mapping={len(mapping)} rows, jsonl={len(queries)} rows, "
        f"schema={args.schema}, vocab_schema={args.vocab_schema}, "
        f"descendants={args.include_descendants}, resident_only={args.resident_only}"
    )

    results: List[GoldEntry] = []
    t0 = time.perf_counter()
    for i, row in enumerate(mapping, 1):
        qid = (row.get("id") or "").upper()
        if only_set and qid not in only_set:
            continue
        qobj = queries.get(qid, {"id": qid, "query": row.get("query", "")})
        if not qobj.get("query") and not row.get("query"):
            results.append(
                GoldEntry(
                    id=qid,
                    tier=row.get("tier", "A"),
                    level=row.get("level", ""),
                    pattern=row.get("pattern", ""),
                    query="",
                    gold_standard="ATHENA_RESIDENT_OMOP",
                    gold_slots=split_slots(row.get("gold_slots") or row.get("codeset_ids") or ""),
                    slot_roles=[],
                    seed_concept_ids_by_slot=[],
                    slot_concept_ids=[],
                    gold_concept_ids=[],
                    gold_concept_count=0,
                    gold_cohort_count=None,
                    gold_sql=None,
                    template_used=None,
                    status="error",
                    stage="",
                    notes="query_not_in_jsonl_or_mapping",
                )
            )
            continue
        print(f"[{i}/{len(mapping)}] {qid} {row.get('level','')} {row.get('pattern','')}")
        entry = process_one(
            row,
            qobj,
            db,
            schema=args.schema,
            vocab_schema=args.vocab_schema,
            include_descendants=args.include_descendants,
            resident_only=args.resident_only,
            skip_cohort_count=args.skip_cohort_count,
        )
        results.append(entry)
        print(
            f"    concepts={entry.gold_concept_count} cohort={entry.gold_cohort_count if entry.gold_cohort_count is not None else '—'} "
            f"status={entry.status} template={entry.template_used or '—'}"
        )

    elapsed = time.perf_counter() - t0
    summary = {
        "dataset_version": DATASET_VERSION,
        "mapping_csv": str(args.mapping_csv),
        "jsonl": str(args.jsonl),
        "schema": args.schema,
        "gold_standard": "ATHENA_OMOP_RESIDENT_STANDARD",
        "vocab_schema": args.vocab_schema,
        "gold_standard": "ATHENA_RESIDENT_OMOP",
        "include_descendants": args.include_descendants,
        "resident_only": args.resident_only,
        "n_total": len(results),
        "counts_by_tier": {tier: sum(1 for r in results if r.tier == tier) for tier in sorted({r.tier for r in results})},
        "n_intentional_tier_b_zero": sum(1 for r in results if r.tier == "B" and r.gold_cohort_count == 0),
        "n_ok": sum(1 for r in results if r.status == "ok"),
        "n_manual_required": sum(1 for r in results if r.status == "manual_required"),
        "n_empty_gold": sum(1 for r in results if r.status == "empty_gold"),
        "n_error": sum(1 for r in results if r.status == "error"),
        "n_concepts_only": sum(1 for r in results if r.status == "concepts_only"),
        "counts_by_level": {lvl: sum(1 for r in results if r.level == lvl) for lvl in sorted({r.level for r in results})},
        "elapsed_seconds": round(elapsed, 2),
    }
    out_obj = {"summary": summary, "results": [asdict(r) for r in results]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(out_obj, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"[Gold-OMOP-resident] saved: {args.output}")
    for k in ("n_ok", "n_manual_required", "n_empty_gold", "n_error", "n_concepts_only"):
        print(f"  {k}: {summary[k]}")
    print(f"  elapsed: {elapsed:.1f}s")
    print("=" * 60)
    if summary["n_error"] or summary["n_empty_gold"] or summary["n_manual_required"]:
        print("ERROR: gold derivation produced non-OK entries; see JSON for details", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
