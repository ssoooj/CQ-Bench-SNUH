-- Inspect resident OMOP concepts before constructing a CDM-resident benchmark.
-- This script is intended for psql. Replace the schema names below if your
-- clinical CDM or vocabulary schema differs from the SYN-SNUH defaults.
--
-- Default clinical schema: synthetic_snuh_cdm
-- Default vocabulary schema: public

-- 1. Resident standard condition concepts.
COPY (
  SELECT
    co.condition_concept_id::bigint AS concept_id,
    c.concept_name,
    c.domain_id,
    c.vocabulary_id,
    c.standard_concept,
    COUNT(DISTINCT co.person_id)::bigint AS patient_count,
    COUNT(*)::bigint AS record_count,
    MIN(co.condition_start_date)::date AS first_date,
    MAX(co.condition_start_date)::date AS last_date
  FROM synthetic_snuh_cdm.condition_occurrence co
  JOIN public.concept c
    ON c.concept_id = co.condition_concept_id
  WHERE co.condition_concept_id <> 0
    AND c.domain_id = 'Condition'
    AND c.standard_concept = 'S'
    AND c.invalid_reason IS NULL
  GROUP BY co.condition_concept_id, c.concept_name, c.domain_id,
           c.vocabulary_id, c.standard_concept
  ORDER BY patient_count DESC, concept_name
) TO STDOUT WITH (FORMAT CSV, HEADER, DELIMITER E'\t')
\g resident_condition_inventory.tsv


-- 2. Resident standard drug concepts.
COPY (
  SELECT
    de.drug_concept_id::bigint AS concept_id,
    c.concept_name,
    c.domain_id,
    c.vocabulary_id,
    c.standard_concept,
    COUNT(DISTINCT de.person_id)::bigint AS patient_count,
    COUNT(*)::bigint AS record_count,
    MIN(de.drug_exposure_start_date)::date AS first_date,
    MAX(de.drug_exposure_start_date)::date AS last_date
  FROM synthetic_snuh_cdm.drug_exposure de
  JOIN public.concept c
    ON c.concept_id = de.drug_concept_id
  WHERE de.drug_concept_id <> 0
    AND c.domain_id = 'Drug'
    AND c.standard_concept = 'S'
    AND c.invalid_reason IS NULL
    AND c.vocabulary_id IN ('RxNorm', 'RxNorm Extension')
  GROUP BY de.drug_concept_id, c.concept_name, c.domain_id,
           c.vocabulary_id, c.standard_concept
  ORDER BY patient_count DESC, concept_name
) TO STDOUT WITH (FORMAT CSV, HEADER, DELIMITER E'\t')
\g resident_drug_inventory.tsv


-- 3. Condition-to-drug temporal capability after first diagnosis.
COPY (
  WITH dx AS (
    SELECT
      condition_concept_id,
      person_id,
      MIN(condition_start_date)::date AS dx_date
    FROM synthetic_snuh_cdm.condition_occurrence
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
    MIN(de.drug_exposure_start_date)::date AS first_drug_date,
    MAX(de.drug_exposure_start_date)::date AS last_drug_date
  FROM dx
  JOIN synthetic_snuh_cdm.drug_exposure de
    ON de.person_id = dx.person_id
   AND de.drug_exposure_start_date >= dx.dx_date
   AND de.drug_exposure_start_date <= dx.dx_date + INTERVAL '365 days'
  JOIN public.concept cc
    ON cc.concept_id = dx.condition_concept_id
  JOIN public.concept dc
    ON dc.concept_id = de.drug_concept_id
  WHERE de.drug_concept_id <> 0
    AND dc.domain_id = 'Drug'
    AND dc.standard_concept = 'S'
    AND dc.invalid_reason IS NULL
    AND dc.vocabulary_id IN ('RxNorm', 'RxNorm Extension')
    AND cc.domain_id = 'Condition'
    AND cc.standard_concept = 'S'
    AND cc.invalid_reason IS NULL
  GROUP BY dx.condition_concept_id, cc.concept_name,
           de.drug_concept_id, dc.concept_name
  ORDER BY n_365d DESC, condition_name, drug_name
) TO STDOUT WITH (FORMAT CSV, HEADER, DELIMITER E'\t')
\g resident_condition_to_drug_after_dx.tsv
