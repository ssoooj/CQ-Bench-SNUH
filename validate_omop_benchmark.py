"""Validate generated OMOP executable-reference benchmark outputs.

Usage:
  python validate_omop_benchmark.py \
    --mapping outputs/omop_executable_benchmark/omop_benchmark_mapping.csv \
    --gold outputs/omop_executable_benchmark/omop_reference_counts.json \
    --capability outputs/omop_executable_benchmark/omop_capability_matrix.json
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

EXPECTED_LEVELS = {"L1": 12, "L2": 18, "L3": 14, "L4": 22, "L5": 14, "TierB": 20}
EXPECTED_TIERS = {"A": 80, "B": 20}


def split_slots(s: str) -> list[str]:
    return [x.strip() for x in re.split(r"\s*\|\s*", s or "") if x.strip()]


def fail(msg: str, failures: list[str]) -> None:
    failures.append(msg)
    print(f"FAIL  {msg}")


def warn(msg: str, warnings: list[str]) -> None:
    warnings.append(msg)
    print(f"WARN  {msg}")


def ok(msg: str) -> None:
    print(f"OK    {msg}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mapping", type=Path, required=True)
    ap.add_argument("--gold", type=Path, required=True)
    ap.add_argument("--capability", type=Path, default=None)
    args = ap.parse_args()

    failures: list[str] = []
    warnings: list[str] = []

    if not args.mapping.is_file():
        fail(f"mapping file not found: {args.mapping}", failures)
        return 2
    if not args.gold.is_file():
        fail(f"gold file not found: {args.gold}", failures)
        return 2

    rows = list(csv.DictReader(args.mapping.open(encoding="utf-8-sig")))
    gold_obj: dict[str, Any] = json.loads(args.gold.read_text(encoding="utf-8"))
    results = gold_obj.get("results", [])
    summary = gold_obj.get("summary", {})

    # Shape checks
    if len(rows) == 100:
        ok("mapping has 100 rows")
    else:
        fail(f"mapping row count is {len(rows)}, expected 100", failures)
    if len(results) == 100:
        ok("gold has 100 results")
    else:
        fail(f"gold result count is {len(results)}, expected 100", failures)

    ids = [r.get("id", "") for r in rows]
    expected_ids = [f"F{i:03d}" for i in range(1, 101)]
    if ids == expected_ids:
        ok("IDs are exactly F001..F100 in order")
    else:
        fail("IDs are not exactly F001..F100 in order", failures)

    tier_counts = Counter(r.get("tier") for r in rows)
    level_counts = Counter(r.get("level") for r in rows)
    if dict(tier_counts) == EXPECTED_TIERS:
        ok(f"tier counts match {EXPECTED_TIERS}")
    else:
        fail(f"tier counts {dict(tier_counts)} != {EXPECTED_TIERS}", failures)
    if dict(level_counts) == EXPECTED_LEVELS:
        ok(f"level counts match {EXPECTED_LEVELS}")
    else:
        fail(f"level counts {dict(level_counts)} != {EXPECTED_LEVELS}", failures)

    # Gold summary checks
    if summary.get("n_total") == 100 and summary.get("n_ok") == 100 and summary.get("n_error") == 0:
        ok("gold summary reports n_total=100, n_ok=100, n_error=0")
    else:
        fail(f"gold summary unexpected: n_total={summary.get('n_total')} n_ok={summary.get('n_ok')} n_error={summary.get('n_error')}", failures)
    if summary.get("counts_by_level") == EXPECTED_LEVELS:
        ok("gold summary counts_by_level matches expected")
    else:
        fail(f"gold summary counts_by_level={summary.get('counts_by_level')} != {EXPECTED_LEVELS}", failures)
    if "tierb" not in str(summary.get("dataset_version", "")).lower():
        warn(f"gold dataset_version does not include 'tierb': {summary.get('dataset_version')}", warnings)

    # Mapping/gold consistency
    gold_by_id = {r.get("id"): r for r in results}
    for r in rows:
        fid = r.get("id")
        g = gold_by_id.get(fid)
        if not g:
            fail(f"{fid}: missing in gold results", failures)
            continue
        for k in ("tier", "level", "pattern", "query"):
            if str(r.get(k, "")) != str(g.get(k, "")):
                fail(f"{fid}: mapping/gold {k} mismatch", failures)
        if split_slots(r.get("gold_slots", "")) != list(g.get("gold_slots", [])):
            fail(f"{fid}: mapping/gold gold_slots mismatch", failures)

    # Tier-specific checks
    for g in results:
        fid = g.get("id")
        tier = g.get("tier")
        level = g.get("level")
        query = g.get("query", "")
        count = g.get("gold_cohort_count")
        slots = g.get("gold_slots", [])
        slot_concept_ids = g.get("slot_concept_ids", [])
        meta = g.get("metadata", {})

        if tier == "A":
            if count is None or count <= 0:
                fail(f"{fid}: Tier A count is not positive: {count}", failures)
            if not slots:
                fail(f"{fid}: Tier A has empty gold_slots", failures)
            if not g.get("gold_concept_ids"):
                fail(f"{fid}: Tier A has empty gold_concept_ids", failures)
        elif tier == "B":
            if count != 0:
                fail(f"{fid}: Tier B count is not zero: {count}", failures)
            if slots or g.get("gold_concept_ids") or g.get("gold_concept_count") != 0:
                fail(f"{fid}: Tier B should have empty concepts and concept_count=0", failures)
            if meta.get("requires_temporal") in (True, "True", "true", "1"):
                warn(f"{fid}: Tier B is marked temporal; query={query!r}", warnings)
        else:
            fail(f"{fid}: unknown tier {tier!r}", failures)

        if level == "L4":
            if len(slot_concept_ids) < 2 or not slot_concept_ids[0] or not slot_concept_ids[1]:
                fail(f"{fid}: L4 required condition+drug slots not both resolved; slot_concept_ids={slot_concept_ids}; query={query!r}", failures)
            if re.search(r"burn of mouth", query, flags=re.I):
                fail(f"{fid}: non-drug phrase 'Burn of mouth' appears in L4 query", failures)
        if level == "L5":
            if len(slot_concept_ids) < 2 or not slot_concept_ids[0] or not slot_concept_ids[1]:
                fail(f"{fid}: L5 required condition+drug-class slots not both resolved; slot_concept_ids={slot_concept_ids}; query={query!r}", failures)

    # Optional capability matrix
    if args.capability:
        if not args.capability.is_file():
            warn(f"capability file not found: {args.capability}", warnings)
        else:
            cap = json.loads(args.capability.read_text(encoding="utf-8"))
            if cap.get("counts_by_level") == EXPECTED_LEVELS and cap.get("counts_by_tier") == EXPECTED_TIERS:
                ok("capability matrix shape matches expected")
            else:
                fail(f"capability shape mismatch: levels={cap.get('counts_by_level')} tiers={cap.get('counts_by_tier')}", failures)
            if cap.get("gold_source") != "athena":
                fail(f"capability gold-source flag unexpected: gold_source={cap.get('gold_source')}", failures)

    print("\nSUMMARY")
    print(f"  failures: {len(failures)}")
    print(f"  warnings: {len(warnings)}")
    if failures:
        print("\nFailure list:")
        for x in failures:
            print(f"  - {x}")
    if warnings:
        print("\nWarning list:")
        for x in warnings:
            print(f"  - {x}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
