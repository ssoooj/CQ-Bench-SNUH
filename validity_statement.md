# CQ-Bench-SNUH: Benchmark Construction and Validity Statement

> ⚠️ **Research preview.**  
> This repository is released as part of an ongoing doctoral dissertation  
> (Jeon, Seoul National University, 2026). Interfaces, counts, and documentation may change before the final dissertation release.

CQ-Bench-SNUH is a CDM-resident executable-reference benchmark for
evaluating natural-language cohort definition systems over an OMOP CDM.
Reference labels are deterministic cohort counts from OMOP concept slots
and SQL templates. It is not a chart-reviewed clinical gold standard.

## Scope

This release is tied to SYN-SNUH. The condition aliases, drug aliases,
drug-class definitions, preferred temporal pairs, and Tier-B negative
controls in `syn_snuh_benchmark_config.json` were curated against the
SYN-SNUH resident inventory.

- SYN-SNUH clinical event tables: assumed in `synthetic_snuh_cdm`
- OMOP vocabulary tables (`concept`, `concept_ancestor`): assumed in `public`

## Objective

Evaluate whether a system can reproduce predefined executable cohort
semantics from natural-language queries. The target is functional fidelity
to an OMOP cohort definition, not clinical phenotype validity.

## Construction

1. **Capability matrix.** Query the target OMOP CDM for resident standard
   conditions, RxNorm drugs, demographic strata, year strata, condition-drug
   temporal pairs, and drug-class exclusion candidates.
2. **Query and mapping generation.** Emit natural-language queries only
   from concepts and patterns observed in the capability matrix. Each query
   gets an ID, tier, level, pattern label, and gold slots
   (`athena:<concept_id>`).
3. **Reference-count derivation.** Resolve gold slots through
   `concept_ancestor`, restrict to valid standard concepts, intersect with
   resident concepts, and execute deterministic SQL templates.

## Query Record Schema

Each JSONL row contains both the natural-language query and the metadata used
to derive its executable reference count. The key fields are:

| Field | Role in benchmark construction |
|---|---|
| `id` | Stable query identifier, e.g., `F001`. |
| `query` | Natural-language cohort question given to evaluated systems. |
| `tier` / `level` / `pattern` | Stratum labels describing benchmark design, not model outputs. |
| `condition_name`, `drug_name`, `drug_class` | Readable terms selected from the SYN-SNUH resident inventory or curated candidate lists. |
| `condition_concept_id`, `drug_concept_id`, `drug_concept_ids` | OMOP seed concepts used to construct executable reference cohorts. |
| `codeset_ids`, `gold_slots` | Slot notation consumed by the reference-count derivation script, usually `athena:<concept_id>`. |
| `gold_source` | Concept-slot source label, such as `athena` or `omop_direct`. |
| `requires_temporal`, `gold_temporal_direction`, `requires_numeric` | Flags used for benchmark stratification and audit. |
| `selection_count_basis` | Count observed during benchmark construction and used to select resident executable queries. |
| `notes` | Optional construction note for preferred pairs, drug classes, or negative-control generation. |

`selection_count_basis` and the derived reference counts are executable
benchmark targets. They are not independent clinical chart-review labels.

## Query Strata

| Stratum | Count | Reference semantics |
|---|---:|---|
| L1 | 12 | Condition cohort |
| L2 | 18 | Condition + age/gender |
| L3 | 14 | Condition + first-diagnosis year |
| L4 | 22 | Condition diagnosis followed by drug exposure within a window |
| L5 | 14 | Condition diagnosis followed by absence of a drug/drug-class |
| Tier B | 20 | Negative controls, zero by construction |
| **Total** | **100** | |

## Tier-B Negative Controls

Tier-B categories are clinically meaningful concepts confirmed *absent*
from the SYN-SNUH resident condition and procedure inventory. Each
candidate is alias-checked against `condition_occurrence` and
`procedure_occurrence` concept names; any match rejects the category.
Validated rows are emitted with empty gold slots and a reference count of
zero:

```sql
SELECT 0::bigint AS cohort_count;
```

This does not assert biological impossibility — only absence under the
SYN-SNUH resident inventory at construction time.

## Drug Class Definitions

Drug-class concept sets are curated ingredient lists (e.g., statin =
atorvastatin, rosuvastatin, simvastatin, pravastatin) rather than ATC
graph traversal outputs. This is a deliberate scope choice: drug-class
semantics in this benchmark are fixed reference sets.

## Reference SQL

L1–L3 build a diagnosis CTE:

```sql
WITH dx AS (
  SELECT person_id, MIN(condition_start_date)::date AS dx_date
  FROM <schema>.condition_occurrence
  WHERE condition_concept_id IN (...)
  GROUP BY person_id
)
```

L4 (temporal inclusion):

```sql
WHERE EXISTS (
  SELECT 1 FROM <schema>.drug_exposure de
  WHERE de.person_id = dx.person_id
    AND de.drug_concept_id IN (...)
    AND de.drug_exposure_start_date >= dx.dx_date
    AND de.drug_exposure_start_date <= dx.dx_date + INTERVAL '<N> days'
)
```

L5 (temporal exclusion):

```sql
WHERE NOT EXISTS (
  SELECT 1 FROM <schema>.drug_exposure de
  WHERE de.person_id = dx.person_id
    AND de.drug_concept_id IN (...)
    AND de.drug_exposure_start_date >= dx.dx_date
)
```

## Temporal Boundary Convention

CQ-Bench-SNUH distinguishes strict sequence, lower-only after, and
bounded post-anchor windows.

In L4, "within N days after diagnosis" is operationalized as an inclusive
post-diagnosis bounded window:
`dx_date ≤ drug_exposure_start_date ≤ dx_date + window`.
This convention intentionally includes same-day diagnosis and prescription
events, which are common in feasibility-oriented EHR cohort queries.

In L5, post-diagnosis drug exclusion is operationalized as the absence of
a drug exposure on or after the first recorded diagnosis date, implemented
by an anchor-bound correlated NOT EXISTS predicate.

CQ-Bench-SNUH L4/L5 therefore evaluate inclusive anchored post-diagnosis
window and temporal non-existence semantics, not strict-after semantics.

## What This Benchmark Is and Is Not

**Evaluates:** natural-language interpretation, OMOP concept grounding,
temporal inclusion and exclusion semantics, functional fidelity of final
cohort counts.

**Does not evaluate:** chart-reviewed phenotype validity, true clinical
absence for Tier-B, treatment effects, dataset-independent prevalence.

## Contact

Sohyeon Jeon · Vital Lab, Healthcare AI Institute, Seoul National 
University Hospital · sohyeon@snu.ac.kr
