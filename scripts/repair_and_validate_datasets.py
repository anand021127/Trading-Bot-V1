#!/usr/bin/env python3
"""Standalone Historical Dataset Repair & Multi-Tier Validation CLI.

Validates and guarantees the integrity of all 6 standard Indian market index datasets
in the real_data/ directory against three strict tiers:
  Tier A: Syntactic JSON Validity (no JSONDecodeError, complete JSON array)
  Tier B: OHLCV Structural Schema (strictly increasing timestamps, 0 duplicates,
          valid floats, low <= open/close <= high, non-negative volume)
  Tier C: Declared Annual Span Coverage (>=15,000 bars, spans 2024-01-01 to 2024-11-06 / 2024-10-31)

Usage:
  python3 scripts/repair_and_validate_datasets.py
  python3 scripts/repair_and_validate_datasets.py --verify-only
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Any, Dict, List, Tuple

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.backtest.historical_data_io import (
    DECLARED_DATASET_SPECS,
    HistoricalDataCorruptedError,
    HistoricalDataError,
    HistoricalDataIncompleteError,
    HistoricalDataValidationError,
    load_dataset_safe,
    salvage_truncated_json,
    save_dataset_atomic,
    validate_candle_record,
    validate_declared_dataset,
    validate_dataset_structure,
)


def inspect_single_file(file_path: str) -> Dict[str, Any]:
    """Perform independent, multi-tier inspection on a single dataset file."""
    result: Dict[str, Any] = {
        "filename": os.path.basename(file_path),
        "file_path": file_path,
        "exists": os.path.exists(file_path),
        "size_bytes": 0,
        "tier_a_json": False,
        "tier_b_ohlcv": False,
        "tier_b_strictly_sorted": False,
        "tier_c_declared_span": False,
        "records_count": 0,
        "first_timestamp": "N/A",
        "last_timestamp": "N/A",
        "error_details": "",
    }

    if not result["exists"]:
        result["error_details"] = "File not found"
        return result

    result["size_bytes"] = os.path.getsize(file_path)

    # Tier A: Raw JSON loading
    try:
        with open(file_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        result["tier_a_json"] = True
    except Exception as e:
        result["error_details"] = f"JSON parse error: {e}"
        return result

    if not isinstance(data, list):
        result["error_details"] = f"Top level is {type(data).__name__}, expected list"
        return result

    result["records_count"] = len(data)
    if len(data) == 0:
        result["error_details"] = "Empty dataset (0 records)"
        return result

    result["first_timestamp"] = str(data[0].get("timestamp", "N/A"))
    result["last_timestamp"] = str(data[-1].get("timestamp", "N/A"))

    # Tier B: Structural OHLCV & Chronological Sorting
    is_valid_struct, struct_msg = validate_dataset_structure(data, min_records=1)
    if is_valid_struct:
        result["tier_b_ohlcv"] = True
        result["tier_b_strictly_sorted"] = True
    else:
        result["error_details"] = f"Structural validation failure: {struct_msg}"
        return result

    # Tier C: Declared Span Coverage
    is_valid_decl, decl_msg = validate_declared_dataset(file_path, data)
    if is_valid_decl:
        result["tier_c_declared_span"] = True
    else:
        result["error_details"] = f"Declared span failure: {decl_msg}"

    return result


def print_validation_matrix(results: List[Dict[str, Any]]) -> bool:
    """Print formatted validation matrix. Returns True if all files passed all tiers."""
    all_passed = True
    print("\n" + "=" * 110)
    print(f"{'DATASET FILENAME':<28} | {'SIZE (MB)':<9} | {'RECORDS':<8} | {'FIRST TS':<16} | {'LAST TS':<16} | {'TIER A':<6} | {'TIER B':<6} | {'TIER C':<6}")
    print("-" * 110)

    for r in results:
        size_mb = f"{r['size_bytes'] / (1024 * 1024):.2f} MB" if r['exists'] else "0 MB"
        tier_a = "PASS" if r['tier_a_json'] else "FAIL"
        tier_b = "PASS" if (r['tier_b_ohlcv'] and r['tier_b_strictly_sorted']) else "FAIL"
        tier_c = "PASS" if r['tier_c_declared_span'] else "FAIL"

        first_ts_short = r['first_timestamp'][:16]
        last_ts_short = r['last_timestamp'][:16]

        is_ok = r['tier_a_json'] and r['tier_b_ohlcv'] and r['tier_b_strictly_sorted'] and r['tier_c_declared_span']
        if not is_ok:
            all_passed = False

        print(f"{r['filename']:<28} | {size_mb:<9} | {r['records_count']:<8} | {first_ts_short:<16} | {last_ts_short:<16} | {tier_a:<6} | {tier_b:<6} | {tier_c:<6}")
        if r['error_details']:
            print(f"   ↳ [DIAGNOSTIC]: {r['error_details']}")

    print("=" * 110 + "\n")
    return all_passed


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair and Validate Historical Datasets")
    parser.add_argument("--data-dir", default=os.path.join(PROJECT_ROOT, "real_data"), help="Directory containing dataset files")
    parser.add_argument("--verify-only", action="store_true", help="Only verify without attempting repair")
    args = parser.parse_args()

    data_dir = os.path.abspath(args.data_dir)
    print(f"Target datasets directory: {data_dir}")

    target_files = sorted(list(DECLARED_DATASET_SPECS.keys()))
    results = []

    for filename in target_files:
        file_path = os.path.join(data_dir, filename)
        inspection = inspect_single_file(file_path)

        if not args.verify_only:
            # If inspection failed Tier A, attempt recovery / safe reload
            if not (inspection["tier_a_json"] and inspection["tier_b_ohlcv"] and inspection["tier_c_declared_span"]):
                print(f"[REPAIR] Attempting safe reload/recovery for {filename}...")
                try:
                    loaded = load_dataset_safe(file_path, auto_repair=True, enforce_declared_specs=False)
                    # Atomically save valid structural data
                    save_dataset_atomic(file_path, loaded, indent=2, enforce_declared_specs=False)
                    # Re-inspect
                    inspection = inspect_single_file(file_path)
                except Exception as e:
                    print(f"  ↳ Could not automatically repair {filename}: {e}")

        results.append(inspection)

    all_passed = print_validation_matrix(results)
    if all_passed:
        print("✓ All 6 datasets verified: 100% compliant with Tier A (JSON), Tier B (OHLCV), and Tier C (Declared Spans).")
        return 0
    else:
        print("✗ One or more datasets failed multi-tier validation.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
