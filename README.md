# CQ-Bench-SNUH

> ⚠️ **Research preview.**  
> This repository is released as part of an ongoing doctoral dissertation  
> (Jeon, Seoul National University, 2026). Interfaces, counts, and documentation may change before the final dissertation release.

CQ-Bench-SNUH is a CDM-resident executable-reference benchmark for evaluating
natural-language cohort definition systems over an OMOP CDM. Reference labels
are deterministic cohort counts from OMOP concept slots and SQL templates.
It is not a chart-reviewed clinical gold standard.

## Reference Dataset

This benchmark was built against the SYN-SNUH (Synthetic Seoul National
University Hospital OMOP CDM) instance, distributed through KHDP:  
[https://khdp.net/database/data-search-detail/SYN-SNUH](https://khdp.net/database/data-search-detail/SYN-SNUH)

To reproduce, register for KHDP access, download SYN-SNUH, and load it into
a local PostgreSQL instance.

## Scope

This release is tied to SYN-SNUH. The condition aliases, drug aliases,
drug-class definitions, preferred temporal pairs, and Tier-B negative
controls in `syn_snuh_benchmark_config.json` were curated against the
SYN-SNUH resident inventory.

- SYN-SNUH clinical event tables: assumed in `synthetic_snuh_cdm`
- OMOP vocabulary tables (`concept`, `concept_ancestor`): assumed in `public`

## Files

## Files

| File | Purpose |
|---|---|
| `feasibility_queries_cqbench_snuh.jsonl` | Released CQ-Bench-SNUH query set containing natural-language cohort questions and executable-reference metadata |
| `validity_statement.md` | Methodological statement defining benchmark scope, construction logic, reference semantics, and limitations |
| `build_omop_executable_benchmark.py` | Build the CDM-resident capability matrix and generate benchmark query/mapping artifacts from SYN-SNUH |
| `derive_omop_reference_counts.py` | Resolve OMOP/Athena gold slots and compute deterministic executable-reference cohort counts |
| `validate_omop_benchmark.py` | Validate benchmark shape, ID order, tier/level counts, mapping-gold consistency, and Tier-specific invariants |
| `syn_snuh_benchmark_config.json` | SYN-SNUH-specific configuration for condition/drug aliases, drug classes, L4/L5 candidates, query templates, and Tier-B negative controls |
| `inspect_resident_inventory.sql` | psql helper for exporting resident condition concepts, resident RxNorm drug concepts, and post-diagnosis condition-to-drug temporal capability |
| `CITATION.cff` | Preferred citation metadata for citing CQ-Bench-SNUH in academic work |

## Query JSONL Schema

Each line in the query JSONL file is one benchmark query plus the metadata
needed to derive its executable reference count. Example:

```json
{
  "id": "F075",
  "query": "How many patients with hyperlipidemia had no prescription of any statin after diagnosis?",
  "tier": "A",
  "level": "L5",
  "pattern": "Condition AFTER_DX NOT DrugClass",
  "condition_name": "hyperlipidemia",
  "condition_concept_id": 432867,
  "drug_class": "statin",
  "drug_concept_ids": "1510813,1539403,1545958,1551860",
  "requires_temporal": true,
  "gold_temporal_direction": "after",
  "requires_numeric": false,
  "codeset_ids": "athena:432867 | athena:1510813,1539403,1545958,1551860",
  "gold_slots": "athena:432867 | athena:1510813,1539403,1545958,1551860",
  "gold_source": "athena",
  "selection_count_basis": 245,
  "notes": "preferred_not_class"
}
```

| Field | Meaning |
|---|---|
| `id` | Stable benchmark query ID. |
| `query` | Natural-language query submitted to a cohort-query system. |
| `tier` | `A` for executable positive queries; `B` for negative-control queries. |
| `level` | Query stratum, from simple condition counts (`L1`) to temporal exclusion (`L5`) or `TierB`. |
| `pattern` | Template family used to generate the query. |
| `condition_name`, `drug_name`, `drug_class` | Readable clinical terms used in the query when applicable. |
| `*_concept_id`, `*_concept_ids` | OMOP seed concept ID or comma-separated seed concept set used to construct the reference cohort. |
| `requires_temporal` | Whether the query requires anchored temporal logic. |
| `gold_temporal_direction` | Temporal direction used by the executable reference template, e.g., `after`. |
| `requires_numeric` | Whether the query requires a numeric threshold. Current release is count-oriented and does not use numeric lab thresholds. |
| `codeset_ids` | Human-readable concept-slot shorthand, usually `athena:<concept_id>`. |
| `gold_slots` | Concept slots consumed by `derive_omop_reference_counts.py`; multi-slot queries use multiple OMOP/Athena seeds. |
| `gold_source` | Source of the concept slot representation, currently `athena` or `omop_direct`. |
| `selection_count_basis` | CDM-resident count observed during benchmark construction; used for query selection and audit, not as chart-reviewed clinical truth. |
| `notes` | Optional construction note, such as whether the query came from a preferred temporal pair or drug-class candidate. |

The JSONL therefore stores a natural-language query together with OMOP concept
seeds and executable-reference metadata. It should not be interpreted as a
clinical adjudication file.

## Typical Workflow

### 1. Build benchmark artifacts

```bash
python build_omop_executable_benchmark.py \
  --schema synthetic_snuh_cdm \
  --vocab-schema public \
  --config syn_snuh_benchmark_config.json \
  --gold-source athena \
  --out-dir outputs/omop_executable_benchmark

python derive_omop_reference_counts.py \
  --schema synthetic_snuh_cdm \
  --vocab-schema public \
  --mapping-csv outputs/omop_executable_benchmark/omop_benchmark_mapping.csv \
  --jsonl outputs/omop_executable_benchmark/omop_benchmark_queries.jsonl \
  --output outputs/omop_executable_benchmark/omop_reference_counts.json

python validate_omop_benchmark.py \
  --mapping outputs/omop_executable_benchmark/omop_benchmark_mapping.csv \
  --gold outputs/omop_executable_benchmark/omop_reference_counts.json \
  --capability outputs/omop_executable_benchmark/omop_capability_matrix.json
```
## Database Connector

The current scripts use a project-specific connector (`ConfigLoader`,
`DBConnector`). For external reuse, replace with any PostgreSQL adapter
exposing `fetch_scalar(sql)` and equivalent row-fetch methods.

## Data Availability and Restrictions

This repository does not include patient-level SYN-SNUH records. The released benchmark artifacts contain natural-language query specifications, OMOP concept-slot metadata, deterministic reference counts, and construction scripts.

Use or reproduction of the benchmark requires independent authorization to access SYN-SNUH through KHDP or an equivalent approved institutional route. Users are responsible for complying with KHDP terms and any applicable institutional policies.

## License

This repository is licensed under the GNU General Public License v3.0.

The license applies only to original materials authored for CQ-Bench-SNUH, including source code, benchmark generation scripts, benchmark query files, reference-count derivation scripts, configuration files, and documentation, unless otherwise noted.

This repository does not grant access to, or redistribution rights for, Synthetic SNUH, KHDP-hosted data, OMOP vocabularies, or any third-party resources. Reproduction requires independent authorized access to the relevant Synthetic SNUH OMOP CDM environment through KHDP or an equivalent approved institutional route.

No patient-level SYN-SNUH records are included in this repository.

## Contact

Sohyeon Jeon · Vital Lab, Healthcare AI Institute, Seoul National 
University Hospital · sohyeon@snu.ac.kr
