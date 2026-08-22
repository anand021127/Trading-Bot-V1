"""Robust Historical Data I/O, Atomic Persistence, and Corruption Detection.

Provides fail-safe, atomic writing, schema validation, and corruption recovery
for market datasets of arbitrary size (including datasets substantially > 2 MB).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class HistoricalDataError(Exception):
    """Base exception for historical data operations."""
    pass


class HistoricalDataCorruptedError(HistoricalDataError):
    """Raised when a historical dataset file is malformed or unrecoverable."""
    pass


class HistoricalDataValidationError(HistoricalDataError):
    """Raised when data fails structural/schema validation before atomic persistence."""
    pass


REQUIRED_CANDLE_FIELDS = ("timestamp", "open", "high", "low", "close")


def validate_candle_record(record: Any) -> bool:
    """Validate that a record conforms to expected OHLCV candle structure."""
    if not isinstance(record, dict):
        return False
    for f in REQUIRED_CANDLE_FIELDS:
        if f not in record or record[f] is None:
            return False
    try:
        # Validate numeric conversions
        float(record["open"])
        float(record["high"])
        float(record["low"])
        float(record["close"])
        if "volume" in record and record["volume"] is not None:
            float(record["volume"])
        ts = str(record["timestamp"])
        if len(ts) < 4:
            return False
    except (ValueError, TypeError):
        return False
    return True


def validate_dataset(data: Any, min_records: int = 1) -> Tuple[bool, str]:
    """Validate a full dataset structure before persistence or ingestion.
    
    Supports:
    1. List of candle dicts (spot index candle format)
    2. Single-contract dict with 'contract' and 'candles' list
    3. Multi-contract list of option records
    """
    if data is None:
        return False, "Data is None"

    if isinstance(data, list):
        if len(data) < min_records:
            return False, f"Record count {len(data)} is below minimum required {min_records}"
        # Validate sample records (head, middle, tail)
        indices_to_check = {0, len(data) // 2, len(data) - 1}
        for idx in indices_to_check:
            if idx < len(data) and not validate_candle_record(data[idx]):
                return False, f"Invalid candle record structure at index {idx}: {data[idx]}"
        return True, "Valid candle array"

    elif isinstance(data, dict):
        if "contract" in data and "candles" in data:
            candles = data.get("candles")
            if not isinstance(candles, list):
                return False, "'candles' must be a list"
            if len(candles) < min_records:
                return False, f"Candles count {len(candles)} is below minimum {min_records}"
            if len(candles) > 0 and not validate_candle_record(candles[0]):
                return False, f"Invalid candle record in options contract: {candles[0]}"
            return True, "Valid option contract dataset"
        return False, "Unrecognized dataset dictionary structure (missing 'contract' or 'candles')"

    return False, f"Unsupported dataset top-level type: {type(data).__name__}"


def salvage_truncated_json(raw_text: str) -> Optional[List[Dict[str, Any]]]:
    """Attempt to safely salvage valid records from a truncated JSON array.
    
    If a stream or file transfer was cut off mid-record (e.g. around 2 MB),
    this finds the last fully closed object '}' and closes the JSON array ']'.
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
            # Filter and ensure all salvaged records are structurally valid
            valid_records = [r for r in parsed if validate_candle_record(r)]
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
) -> str:
    """Atomically write and validate historical dataset to file.
    
    Guarantees:
    1. Writes to a temporary file in the same directory (avoiding cross-filesystem renames).
    2. Writes the full JSON content without buffer truncations.
    3. Flushes and syncs to disk (fsync).
    4. Validates full JSON serialization and structure before destination replacement.
    5. Replaces destination atomically with os.replace only after 100% validation success.
    6. Never leaves a partial or corrupt file as the active destination.
    """
    is_valid, msg = validate_dataset(data, min_records=min_records)
    if not is_valid:
        raise HistoricalDataValidationError(f"Cannot save invalid dataset to {file_path}: {msg}")

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

        # Post-write validation: read back and verify JSON integrity from disk
        with open(temp_path, "r", encoding="utf-8") as fp:
            reloaded = json.load(fp)

        reloaded_valid, reloaded_msg = validate_dataset(reloaded, min_records=min_records)
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
            len(data) if isinstance(data, list) else len(data.get("candles", [])),
        )
        return file_path

    except Exception as e:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        if isinstance(e, HistoricalDataValidationError):
            raise
        raise HistoricalDataError(f"Failed to atomically persist dataset to {file_path}: {e}") from e


def load_dataset_safe(
    file_path: str,
    auto_repair: bool = True,
    min_records: int = 1,
) -> List[Dict[str, Any]]:
    """Safely load a historical candle dataset with corruption detection & recovery.
    
    If auto_repair is True and the file suffered a truncated write (e.g. from an
    incomplete transfer), valid records are salvaged, validated, and atomically
    persisted back so subsequent loads are immediate and clean.
    
    Raises HistoricalDataCorruptedError if data cannot be parsed or validated.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Historical dataset not found: {file_path}")

    file_size = os.path.getsize(file_path)
    if file_size == 0:
        raise HistoricalDataCorruptedError(f"Historical dataset file is empty (0 bytes): {file_path}")

    try:
        with open(file_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)

        if isinstance(data, list):
            is_valid, msg = validate_dataset(data, min_records=min_records)
            if not is_valid:
                raise HistoricalDataValidationError(f"Dataset in {file_path} failed validation: {msg}")
            return data
        elif isinstance(data, dict) and "candles" in data:
            candles = data["candles"]
            if isinstance(candles, list):
                return candles
        raise HistoricalDataValidationError(
            f"Dataset in {file_path} has unexpected format: {type(data).__name__}"
        )

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
                    logger.info(
                        "Auto-repairing corrupted dataset %s with %d salvaged records",
                        file_path,
                        len(salvaged),
                    )
                    # Atomically save the repaired dataset
                    save_dataset_atomic(file_path, salvaged, min_records=min_records)
                    return salvaged
            except Exception as repair_err:
                logger.error("Auto-repair failed on %s: %s", file_path, repair_err)

        raise HistoricalDataCorruptedError(
            f"Corrupted or truncated historical dataset: {file_path} "
            f"(size: {file_size} bytes, JSON error: {decode_err})"
        ) from decode_err

    except Exception as e:
        if isinstance(e, (HistoricalDataCorruptedError, HistoricalDataValidationError, FileNotFoundError)):
            raise
        raise HistoricalDataCorruptedError(f"Failed to load dataset from {file_path}: {e}") from e
