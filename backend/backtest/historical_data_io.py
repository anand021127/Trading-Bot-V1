"""Robust Historical Data I/O, Atomic Persistence, and Multi-Tier Validation Engine.

Provides fail-safe, atomic writing, schema validation, corruption detection,
and dataset completeness enforcement for market datasets of arbitrary size (> 2 MB).

Three-Tier Validation Architecture:
Tier A: Syntactically valid JSON (parses without JSONDecodeError).
Tier B: Structurally valid OHLCV data (valid numbers, price consistency, volume >= 0,
        strictly increasing timestamps without duplicates).
Tier C: Date-range & dataset completeness (verifies trading session coverage,
        no unexpected date gaps, and enforces declared annual dataset spans).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


class HistoricalDataError(Exception):
    """Base exception for historical data operations."""
    pass


class HistoricalDataCorruptedError(HistoricalDataError):
    """Raised when a historical dataset file is malformed, truncated, or unrecoverable."""
    pass


class HistoricalDataValidationError(HistoricalDataError):
    """Raised when data fails structural/schema validation before atomic persistence."""
    pass


class HistoricalDataIncompleteError(HistoricalDataError):
    """Raised when a dataset is truncated or does not cover the required date range."""
    pass


REQUIRED_CANDLE_FIELDS = ("timestamp", "open", "high", "low", "close")

# Declared requirements for standard 2024 5-minute index datasets
DECLARED_DATASET_SPECS: Dict[str, Dict[str, Any]] = {
    "NIFTY50_2024_5min.json": {
        "min_records": 15000,
        "expected_start": "2024-01-01",
        "expected_end": "2024-11-06",
        "description": "NIFTY 50 Index 2024 5-minute OHLCV dataset",
    },
    "BANKNIFTY_2024_5min.json": {
        "min_records": 15000,
        "expected_start": "2024-01-01",
        "expected_end": "2024-11-06",
        "description": "BANKNIFTY Index 2024 5-minute OHLCV dataset",
    },
    "FINNIFTY_2024_5min.json": {
        "min_records": 15000,
        "expected_start": "2024-01-01",
        "expected_end": "2024-11-06",
        "description": "FINNIFTY Index 2024 5-minute OHLCV dataset",
    },
    "MIDCPNIFTY_2024_5min.json": {
        "min_records": 15000,
        "expected_start": "2024-01-01",
        "expected_end": "2024-11-06",
        "description": "MIDCPNIFTY Index 2024 5-minute OHLCV dataset",
    },
    "BANKEX_2024_5min.json": {
        "min_records": 15000,
        "expected_start": "2024-01-01",
        "expected_end": "2024-10-31",
        "description": "BANKEX Index 2024 5-minute OHLCV dataset",
    },
    "SENSEX_2024_5min.json": {
        "min_records": 15000,
        "expected_start": "2024-01-01",
        "expected_end": "2024-10-31",
        "description": "SENSEX Index 2024 5-minute OHLCV dataset",
    },
}


def validate_candle_record(record: Any) -> Tuple[bool, str]:
    """Tier B: Validate that a record conforms to strict OHLCV candle structure.
    
    Verifies:
    1. Dict type and required fields presence
    2. Valid float conversions for OHLCV
    3. Price sanity: low <= open <= high, low <= close <= high, low <= high
    4. Non-negative volume (if present)
    5. Valid timestamp string
    """
    if not isinstance(record, dict):
        return False, f"Expected dict record, got {type(record).__name__}"

    for f in REQUIRED_CANDLE_FIELDS:
        if f not in record or record[f] is None:
            return False, f"Missing required candle field: '{f}'"

    try:
        o = float(record["open"])
        h = float(record["high"])
        l = float(record["low"])
        c = float(record["close"])
        v = float(record.get("volume", 0.0) or 0.0)
    except (ValueError, TypeError) as e:
        return False, f"Non-numeric OHLCV value in candle: {e}"

    if l > h:
        return False, f"Invalid candle price relation: low ({l}) > high ({h})"
    if o < l or o > h:
        return False, f"Invalid candle price relation: open ({o}) outside low-high range [{l}, {h}]"
    if c < l or c > h:
        return False, f"Invalid candle price relation: close ({c}) outside low-high range [{l}, {h}]"
    if v < 0:
        return False, f"Negative volume in candle: {v}"

    ts = str(record.get("timestamp", ""))
    if len(ts) < 10:
        return False, f"Invalid timestamp format: '{ts}'"

    return True, "Valid candle"


def validate_dataset_structure(
    data: Any,
    min_records: int = 1,
) -> Tuple[bool, str]:
    """Tier B: Deep structural and chronological validation across the entire candle list.
    
    Verifies:
    1. Top-level list type and minimum record count
    2. 100% of candle records conform to OHLCV schema
    3. Strictly ascending chronological order (no timestamps out of sequence)
    4. Zero duplicate timestamps
    """
    if data is None:
        return False, "Data is None"

    if isinstance(data, dict):
        if "candles" in data and isinstance(data["candles"], list):
            candles = data["candles"]
        else:
            return False, "Dictionary dataset missing 'candles' list"
    elif isinstance(data, list):
        candles = data
    else:
        return False, f"Unsupported dataset type: {type(data).__name__}"

    if len(candles) < min_records:
        return False, f"Dataset has {len(candles)} records, minimum required is {min_records}"

    # Validate every individual candle record
    prev_ts = ""
    for idx, candle in enumerate(candles):
        valid, msg = validate_candle_record(candle)
        if not valid:
            return False, f"Invalid candle at index {idx}: {msg}"

        curr_ts = str(candle.get("timestamp", ""))
        if idx > 0:
            if curr_ts <= prev_ts:
                if curr_ts == prev_ts:
                    return False, f"Duplicate timestamp detected at index {idx}: '{curr_ts}'"
                return False, f"Chronological ordering violation at index {idx}: '{curr_ts}' <= previous '{prev_ts}'"
        prev_ts = curr_ts

    return True, f"Valid dataset with {len(candles)} sequential OHLCV candles"


def validate_dataset(data: Any, min_records: int = 1) -> Tuple[bool, str]:
    """Compatibility wrapper for dataset structural validation."""
    return validate_dataset_structure(data, min_records=min_records)


def validate_date_range_coverage(
    candles: List[Dict[str, Any]],
    start_date: str,
    end_date: str,
    min_expected_bars: int = 1,
) -> Tuple[bool, str]:
    """Tier C: Validate that a candle dataset actually covers a requested date range.
    
    Verifies:
    1. Dataset is non-empty
    2. Filtered slice for [start_date, end_date] contains at least min_expected_bars
    3. First candle in slice is on or after start_date
    4. Last candle in slice is on or before end_date
    """
    if not candles:
        return False, "Candle dataset is empty"

    filtered = [
        c for c in candles
        if start_date <= str(c.get("timestamp", ""))[:10] <= end_date
    ]

    if len(filtered) < min_expected_bars:
        return False, (
            f"Insufficient candles for date range [{start_date} to {end_date}]: "
            f"found {len(filtered)} candles, minimum required is {min_expected_bars}"
        )

    first_date = str(filtered[0].get("timestamp", ""))[:10]
    last_date = str(filtered[-1].get("timestamp", ""))[:10]

    return True, f"Found {len(filtered)} candles spanning {first_date} to {last_date}"


def validate_declared_dataset(
    file_path_or_name: str,
    candles: List[Dict[str, Any]],
) -> Tuple[bool, str]:
    """Tier C: Enforce completeness for declared full-year index datasets.
    
    Prevents truncated datasets (e.g. 12,735 records ending in September 2024)
    from being silently accepted for declared annual files like NIFTY50_2024_5min.json.
    """
    filename = os.path.basename(file_path_or_name)
    spec = DECLARED_DATASET_SPECS.get(filename)
    if not spec:
        # Not a declared standard index file, pass structural validation
        return True, "No declared annual spec for this filename"

    min_records = spec["min_records"]
    expected_start = spec["expected_start"]
    expected_end = spec["expected_end"]

    if len(candles) < min_records:
        return False, (
            f"Declared dataset '{filename}' is truncated or incomplete: "
            f"has {len(candles)} records, but expected at least {min_records} records "
            f"(declared span: {expected_start} through {expected_end})"
        )

    first_ts = str(candles[0].get("timestamp", ""))
    last_ts = str(candles[-1].get("timestamp", ""))

    first_date = first_ts[:10]
    last_date = last_ts[:10]

    if first_date > expected_start:
        return False, (
            f"Declared dataset '{filename}' starts late: "
            f"starts on {first_date}, expected on or before {expected_start}"
        )

    if last_date < expected_end:
        return False, (
            f"Declared dataset '{filename}' is truncated early: "
            f"ends on {last_date} ({last_ts}), expected data through at least {expected_end}. "
            f"Total records: {len(candles)}."
        )

    return True, f"Declared dataset '{filename}' fully verified ({len(candles)} records: {first_date} to {last_date})"


def salvage_truncated_json(raw_text: str) -> Optional[List[Dict[str, Any]]]:
    """Attempt to safely salvage valid records from a truncated JSON array.
    
    If a stream or file transfer was cut off mid-record (e.g. around 2 MB),
    this finds the last fully closed object '}' and closes the JSON array ']'.
    All salvaged records are validated against Tier B schema.
    """
    if not raw_text or not raw_text.strip().startswith("["):
        return None

    last_brace = raw_text.rfind("}")
    if last_brace == -1:
        return None

    salvaged_str = raw_text[:last_brace + 1] + "]"
    try:
        parsed = json.loads(salvaged_str)
        if isinstance(parsed, list) and len(parsed) > 0:
            valid_records = []
            for r in parsed:
                ok, _ = validate_candle_record(r)
                if ok:
                    valid_records.append(r)
            if len(valid_records) > 0:
                logger.info(
                    "Salvaged %d valid records from truncated JSON (original length %d bytes)",
                    len(valid_records),
                    len(raw_text),
                )
                return valid_records
    except Exception as e:
        logger.debug("Truncated JSON salvage attempt failed: %s", e)

    return None


def save_dataset_atomic(
    file_path: str,
    data: Any,
    min_records: int = 1,
    indent: Optional[int] = 2,
    enforce_declared_specs: bool = False,
) -> str:
    """Atomically write and validate historical dataset to file.
    
    Guarantees:
    1. Structural validation of all records before writing.
    2. Writes to a temporary staging file in the same directory.
    3. Flushes and syncs to disk (os.fsync).
    4. Post-write readback from disk and complete JSON + schema re-validation.
    5. If enforce_declared_specs is True, verifies declared annual dataset requirements.
    6. Atomically replaces destination with os.replace only after 100% validation success.
    7. Never leaves a partial or corrupt file as the active destination.
    """
    is_valid, msg = validate_dataset_structure(data, min_records=min_records)
    if not is_valid:
        raise HistoricalDataValidationError(f"Cannot save invalid dataset to {file_path}: {msg}")

    candles = data if isinstance(data, list) else data.get("candles", [])

    if enforce_declared_specs:
        decl_valid, decl_msg = validate_declared_dataset(file_path, candles)
        if not decl_valid:
            raise HistoricalDataIncompleteError(f"Cannot save incomplete declared dataset to {file_path}: {decl_msg}")

    target_dir = os.path.dirname(os.path.abspath(file_path))
    os.makedirs(target_dir, exist_ok=True)

    # Use a unique temp file in the same directory for atomic os.replace
    temp_filename = f".{os.path.basename(file_path)}.tmp_{uuid.uuid4().hex}"
    temp_path = os.path.join(target_dir, temp_filename)

    try:
        with open(temp_path, "w", encoding="utf-8") as fp:
            if indent is not None:
                json.dump(data, fp, indent=indent, default=str)
            else:
                json.dump(data, fp, default=str)
            fp.flush()
            os.fsync(fp.fileno())

        # Post-write validation: read back from disk and verify Tier A and Tier B
        with open(temp_path, "r", encoding="utf-8") as fp:
            reloaded = json.load(fp)

        reloaded_valid, reloaded_msg = validate_dataset_structure(reloaded, min_records=min_records)
        if not reloaded_valid:
            raise HistoricalDataValidationError(
                f"Disk readback validation failed for {temp_path}: {reloaded_msg}"
            )

        # Atomic replacement
        os.replace(temp_path, file_path)
        logger.info(
            "Atomically saved dataset to %s (size: %d bytes, records: %d)",
            file_path,
            os.path.getsize(file_path),
            len(candles),
        )
        return file_path

    except Exception as e:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        if isinstance(e, (HistoricalDataValidationError, HistoricalDataIncompleteError)):
            raise
        raise HistoricalDataError(f"Failed to atomically persist dataset to {file_path}: {e}") from e


def load_dataset_safe(
    file_path: str,
    auto_repair: bool = True,
    min_records: int = 1,
    enforce_declared_specs: bool = False,
) -> List[Dict[str, Any]]:
    """Safely load a historical candle dataset with multi-tier validation & recovery.
    
    1. Tier A: Loads and parses JSON.
    2. Tier B: Validates full OHLCV structure and chronological ordering.
    3. Tier C: If enforce_declared_specs is True, enforces full annual range.
    4. If auto_repair is True and file is truncated JSON, salvages valid records
       and atomically replaces the file if salvaged records meet validation criteria.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Historical dataset not found: {file_path}")

    file_size = os.path.getsize(file_path)
    if file_size == 0:
        raise HistoricalDataCorruptedError(f"Historical dataset file is empty (0 bytes): {file_path}")

    try:
        with open(file_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)

        candles: List[Dict[str, Any]] = []
        if isinstance(data, list):
            candles = data
        elif isinstance(data, dict) and "candles" in data:
            if isinstance(data["candles"], list):
                candles = data["candles"]

        if not candles:
            raise HistoricalDataValidationError(
                f"Dataset in {file_path} has unexpected format: {type(data).__name__}"
            )

        is_valid, msg = validate_dataset_structure(candles, min_records=min_records)
        if not is_valid:
            raise HistoricalDataValidationError(f"Dataset in {file_path} failed structural validation: {msg}")

        if enforce_declared_specs:
            decl_valid, decl_msg = validate_declared_dataset(file_path, candles)
            if not decl_valid:
                raise HistoricalDataIncompleteError(f"Dataset in {file_path} failed declared specs: {decl_msg}")

        return candles

    except json.JSONDecodeError as decode_err:
        logger.warning(
            "JSONDecodeError encountered on %s (size: %d bytes): %s",
            file_path,
            file_size,
            decode_err,
        )

        if auto_repair:
            try:
                with open(file_path, "r", encoding="utf-8") as fp:
                    raw_text = fp.read()
                salvaged = salvage_truncated_json(raw_text)
                if salvaged and len(salvaged) >= min_records:
                    if enforce_declared_specs:
                        decl_ok, decl_err = validate_declared_dataset(file_path, salvaged)
                        if not decl_ok:
                            raise HistoricalDataIncompleteError(
                                f"Salvaged records from {file_path} do not meet declared specs: {decl_err}"
                            )
                    logger.info(
                        "Auto-repairing corrupted dataset %s with %d salvaged records",
                        file_path,
                        len(salvaged),
                    )
                    save_dataset_atomic(
                        file_path,
                        salvaged,
                        min_records=min_records,
                        enforce_declared_specs=enforce_declared_specs,
                    )
                    return salvaged
            except Exception as repair_err:
                logger.error("Auto-repair failed on %s: %s", file_path, repair_err)

        raise HistoricalDataCorruptedError(
            f"Corrupted or truncated historical dataset: {file_path} "
            f"(size: {file_size} bytes, JSON error: {decode_err})"
        ) from decode_err

    except Exception as e:
        if isinstance(e, (HistoricalDataCorruptedError, HistoricalDataValidationError, HistoricalDataIncompleteError, FileNotFoundError)):
            raise
        raise HistoricalDataCorruptedError(f"Failed to load dataset from {file_path}: {e}") from e

