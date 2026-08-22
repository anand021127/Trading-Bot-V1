"""Comprehensive unit & integration test suite for historical data pipeline,
atomic persistence, large datasets (> 2 MB), corruption recovery, and backtest task state handling.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta

from backend.backtest.historical_data_io import (
    HistoricalDataCorruptedError,
    HistoricalDataError,
    HistoricalDataValidationError,
    load_dataset_safe,
    salvage_truncated_json,
    save_dataset_atomic,
    validate_candle_record,
    validate_dataset,
)
from backend.backtest.task_manager import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    BacktestTaskManager,
    run_backtest_in_background,
)
from backend.backtest.engine import BacktestEngine, CostConfig
from backend.strategy.strategy_engine import MultiStrategyEngine
from backend.strategy.strategies.ema_trend import EMATrendStrategy


def generate_synthetic_candles(count: int, start_time: str = "2024-01-01T09:15:00+05:30") -> list:
    """Generate deterministic OHLCV candle records."""
    candles = []
    base_dt = datetime.fromisoformat(start_time)
    price = 24000.0
    for i in range(count):
        current_dt = base_dt + timedelta(minutes=5 * i)
        o = round(price + (i % 7) * 1.5, 2)
        h = round(o + 5.0, 2)
        l = round(o - 4.0, 2)
        c = round(o + 1.2, 2)
        v = 1500 + (i % 500)
        candles.append({
            "timestamp": current_dt.isoformat(),
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": v,
        })
        price = c
    return candles


class TestHistoricalDataPipeline(unittest.TestCase):

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="test_hist_data_")

    def tearDown(self) -> None:
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_1_large_dataset_exceeding_2mb(self) -> None:
        """Test dataset substantially > 2 MB can be generated, atomically written, validated, loaded, and processed."""
        # ~25,000 candles is approx 3.8 MB JSON
        candle_count = 25000
        candles = generate_synthetic_candles(candle_count)
        target_path = os.path.join(self.test_dir, "LARGE_NIFTY_5min.json")

        # 1. Atomic save
        saved_path = save_dataset_atomic(target_path, candles, min_records=100)
        self.assertEqual(saved_path, target_path)
        self.assertTrue(os.path.exists(target_path))

        file_size = os.path.getsize(target_path)
        print(f"\n[Test 1] Large dataset size: {file_size} bytes ({file_size / (1024 * 1024):.2f} MB)")
        self.assertGreater(file_size, 2 * 1024 * 1024, "File size must be strictly > 2 MB")

        # 2. Safe load & integrity check
        loaded_candles = load_dataset_safe(target_path, auto_repair=False)
        self.assertEqual(len(loaded_candles), candle_count)
        self.assertEqual(loaded_candles[0]["timestamp"], candles[0]["timestamp"])
        self.assertEqual(loaded_candles[-1]["timestamp"], candles[-1]["timestamp"])

        # 3. Process through BacktestEngine
        engine = BacktestEngine(
            strategy_engine=MultiStrategyEngine([EMATrendStrategy()]),
            costs=CostConfig(),
            capital=100000.0,
        )
        res = engine.run(
            symbol_candles={"NIFTY": loaded_candles[:2000]},
            strategy_names=["EMA_TREND"],
            require_real_options=False,
        )
        self.assertIsNotNone(res)
        self.assertEqual(res.total_candles_scanned, 2000)
        print(f"[Test 1] Processed {res.total_candles_scanned} candles through BacktestEngine successfully.")

    def test_2_truncated_json_salvage_and_recovery(self) -> None:
        """Test explicitly truncated JSON file is detected, salvaged, and atomically repaired."""
        candles = generate_synthetic_candles(1000)
        raw_json = json.dumps(candles, indent=2)

        # Deliberately truncate around byte position 15000 in the middle of a string record
        cut_point = raw_json.find('"open":', 15000) + 3
        truncated_raw = raw_json[:cut_point]

        corrupt_path = os.path.join(self.test_dir, "TRUNCATED_DATA.json")
        with open(corrupt_path, "w", encoding="utf-8") as f:
            f.write(truncated_raw)

        # Verify plain json.loads fails with JSONDecodeError
        with self.assertRaises(json.JSONDecodeError):
            with open(corrupt_path, "r", encoding="utf-8") as f:
                json.load(f)

        # Load with auto_repair enabled
        repaired_candles = load_dataset_safe(corrupt_path, auto_repair=True)
        self.assertGreater(len(repaired_candles), 50)
        print(f"\n[Test 2] Truncated dataset recovered {len(repaired_candles)} complete records.")

        # Verify that subsequent standard json.load now succeeds cleanly
        with open(corrupt_path, "r", encoding="utf-8") as f:
            clean_load = json.load(f)
        self.assertEqual(len(clean_load), len(repaired_candles))

    def test_3_malformed_unrecoverable_json(self) -> None:
        """Test malformed non-JSON data raises HistoricalDataCorruptedError."""
        bad_path = os.path.join(self.test_dir, "MALFORMED.json")
        with open(bad_path, "w", encoding="utf-8") as f:
            f.write("<!DOCTYPE html><html><body>Error 502 Bad Gateway</body></html>")

        with self.assertRaises(HistoricalDataCorruptedError):
            load_dataset_safe(bad_path, auto_repair=True)

    def test_4_atomic_write_failure_preserves_original(self) -> None:
        """Test atomic write failure does not overwrite or corrupt original destination."""
        target_path = os.path.join(self.test_dir, "ORIGINAL_SAFE.json")
        original_candles = generate_synthetic_candles(50)
        save_dataset_atomic(target_path, original_candles)

        # Attempt to save invalid data (e.g. invalid candle record structure)
        invalid_data = [{"bad_field": 123}]
        with self.assertRaises(HistoricalDataValidationError):
            save_dataset_atomic(target_path, invalid_data)

        # Destination must remain untouched with original data
        reloaded = load_dataset_safe(target_path, auto_repair=False)
        self.assertEqual(len(reloaded), 50)
        self.assertEqual(reloaded[0]["timestamp"], original_candles[0]["timestamp"])

    def test_5_atomic_replacement_success(self) -> None:
        """Test successful atomic replacement of an existing dataset."""
        target_path = os.path.join(self.test_dir, "REPLACE_TARGET.json")
        initial_candles = generate_synthetic_candles(100)
        save_dataset_atomic(target_path, initial_candles)

        # Replace with new candles
        updated_candles = generate_synthetic_candles(250)
        save_dataset_atomic(target_path, updated_candles)

        reloaded = load_dataset_safe(target_path, auto_repair=False)
        self.assertEqual(len(reloaded), 250)

    def test_6_backtest_task_state_failure_propagation(self) -> None:
        """Test backtest task state transitions strictly to 'failed' on data/simulation failure."""
        from backend.backtest.task_manager import task_manager
        task = task_manager.create_task()
        task_id = task.task_id

        engine = BacktestEngine(
            strategy_engine=MultiStrategyEngine([EMATrendStrategy()]),
            costs=CostConfig(),
            capital=100000.0,
        )

        # Run background backtest with non-existent symbols and no client -> must fail
        asyncio.run(
            run_backtest_in_background(
                task_id=task_id,
                client=None,
                engine=engine,
                symbols=["NON_EXISTENT_INDEX"],
                interval="5minute",
                start_date="2024-01-01",
                end_date="2024-01-10",
                strategy_names=["EMA_TREND"],
            )
        )

        updated_task = task_manager.get(task_id)
        self.assertIsNotNone(updated_task)
        self.assertEqual(updated_task.status, STATUS_FAILED, "Task status must be 'failed'")
        self.assertIn("DATA_UNAVAILABLE", updated_task.error)
        self.assertIsNone(updated_task.result, "Failed task must not have result payload")
        print(f"\n[Test 6] Backtest failure propagated cleanly with error: {updated_task.error}")

    def test_7_tier_b_structural_validation_catches_errors(self) -> None:
        """Test Tier B catches price inconsistency, duplicate timestamps, and out-of-order candles."""
        candles = generate_synthetic_candles(10)

        # 1. Price inconsistency: low > high
        bad_candles = [dict(c) for c in candles]
        bad_candles[3]["low"] = 25000.0
        bad_candles[3]["high"] = 24000.0
        valid, msg = validate_dataset(bad_candles)
        self.assertFalse(valid)
        self.assertIn("low", msg)

        # 2. Duplicate timestamp
        dup_candles = [dict(c) for c in candles]
        dup_candles[4]["timestamp"] = dup_candles[3]["timestamp"]
        valid, msg = validate_dataset(dup_candles)
        self.assertFalse(valid)
        self.assertIn("Duplicate timestamp", msg)

        # 3. Out of order timestamp
        rev_candles = list(reversed(candles))
        valid, msg = validate_dataset(rev_candles)
        self.assertFalse(valid)
        self.assertIn("Chronological ordering violation", msg)

    def test_8_tier_c_declared_dataset_rejects_early_truncated_data(self) -> None:
        """Test Tier C rejects a 12,735-record dataset ending in September 2024 for NIFTY50_2024_5min.json."""
        from backend.backtest.historical_data_io import validate_declared_dataset, HistoricalDataIncompleteError

        # Generate 12,735 candles ending on 2024-09-09
        truncated_candles = generate_synthetic_candles(12735, start_time="2024-01-01T09:15:00+05:30")
        
        valid, msg = validate_declared_dataset("NIFTY50_2024_5min.json", truncated_candles)
        self.assertFalse(valid)
        self.assertIn("truncated or incomplete", msg)
        print(f"\n[Test 8] Declared annual validator correctly rejected incomplete dataset: {msg}")

        # Attempt to save to declared filename with enforce_declared_specs=True
        target_file = os.path.join(self.test_dir, "NIFTY50_2024_5min.json")
        with self.assertRaises(HistoricalDataIncompleteError):
            save_dataset_atomic(target_file, truncated_candles, enforce_declared_specs=True)

    def test_9_task_manager_progress_reports_exact_total_bars_on_completion(self) -> None:
        """Test completed backtest reports exact total expected bars (e.g. 600/600), not stalling at intermediate step."""
        from backend.backtest.task_manager import task_manager
        task = task_manager.create_task()
        task_id = task.task_id

        # Use real NIFTY50 data from 2024-01-01 to 2024-01-10 (600 bars)
        engine = BacktestEngine(
            strategy_engine=MultiStrategyEngine([EMATrendStrategy()]),
            costs=CostConfig(),
            capital=100000.0,
        )

        asyncio.run(
            run_backtest_in_background(
                task_id=task_id,
                client=None,
                engine=engine,
                symbols=["NIFTY50"],
                interval="5minute",
                start_date="2024-01-01",
                end_date="2024-01-10",
                strategy_names=["EMA_TREND"],
            )
        )

        completed_task = task_manager.get(task_id)
        self.assertIsNotNone(completed_task)
        self.assertEqual(completed_task.status, STATUS_COMPLETED)
        self.assertIsNotNone(completed_task.progress)

        progress = completed_task.progress
        print(f"\n[Test 9] Completed task progress payload: {progress}")
        self.assertEqual(progress.get("phase"), "completed")
        self.assertEqual(progress.get("bar_index"), 600)
        self.assertEqual(progress.get("total_bars"), 600)
        self.assertEqual(progress.get("processed_bars"), 600)
        self.assertEqual(progress.get("expected_bars"), 600)


if __name__ == "__main__":
    unittest.main()
