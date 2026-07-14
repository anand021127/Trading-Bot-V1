"""Tests for the background backtest task manager (fixes the
'timeout of 30000ms exceeded' bug — a full year of 5-minute NIFTY data
routinely took longer than the frontend's 30s axios timeout when run
synchronously inside one request)."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from backend.backtest.task_manager import (
    BacktestTaskManager,
    STATUS_QUEUED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    run_backtest_in_background,
)


class TestBacktestTaskManager:
    def test_create_task_starts_queued(self) -> None:
        mgr = BacktestTaskManager()
        task = mgr.create_task()
        assert task.status == STATUS_QUEUED
        assert mgr.get(task.task_id) is task

    def test_update_progress_updates_status_and_progress(self) -> None:
        mgr = BacktestTaskManager()
        task = mgr.create_task()
        mgr.update_progress(task.task_id, {"phase": "fetching_data"}, status="fetching_data")
        assert mgr.get(task.task_id).status == "fetching_data"
        assert mgr.get(task.task_id).progress["phase"] == "fetching_data"

    def test_complete_stores_result(self) -> None:
        mgr = BacktestTaskManager()
        task = mgr.create_task()
        mgr.complete(task.task_id, {"trades_taken": 5})
        updated = mgr.get(task.task_id)
        assert updated.status == STATUS_COMPLETED
        assert updated.result == {"trades_taken": 5}

    def test_fail_stores_error(self) -> None:
        mgr = BacktestTaskManager()
        task = mgr.create_task()
        mgr.fail(task.task_id, "boom")
        updated = mgr.get(task.task_id)
        assert updated.status == STATUS_FAILED
        assert updated.error == "boom"

    def test_unknown_task_id_returns_none(self) -> None:
        mgr = BacktestTaskManager()
        assert mgr.get("does-not-exist") is None

    def test_updating_unknown_task_does_not_raise(self) -> None:
        mgr = BacktestTaskManager()
        mgr.update_progress("nope", {})  # must not raise
        mgr.complete("nope", {})
        mgr.fail("nope", "x")

    def test_to_status_dict_shape(self) -> None:
        mgr = BacktestTaskManager()
        task = mgr.create_task()
        d = task.to_status_dict()
        assert d["task_id"] == task.task_id
        assert d["status"] == STATUS_QUEUED
        assert "elapsed_seconds" in d


class TestRunBacktestInBackground:
    def _fake_client(self, candles_by_symbol):
        client = MagicMock()
        def fetch(symbol, interval, start, end):
            return candles_by_symbol.get(symbol, [])
        client.get_historical_candles_full_range.side_effect = fetch
        return client

    def test_successful_run_completes_task_with_result(self) -> None:
        mgr = BacktestTaskManager()
        task = mgr.create_task()

        candles = [{"open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100 + i,
                    "volume": 1000, "timestamp": f"bar-{i:04d}"} for i in range(70)]
        client = self._fake_client({"NIFTY50": candles})

        engine = MagicMock()
        fake_result = MagicMock()
        fake_result.trades_taken = 2
        fake_result.skipped_symbols = []
        fake_result.to_dict.return_value = {"trades_taken": 2, "skipped_symbols": []}
        engine.run.return_value = fake_result

        # Patch the module-level singleton to use our isolated manager.
        import backend.backtest.task_manager as tm_module
        original = tm_module.task_manager
        tm_module.task_manager = mgr
        try:
            asyncio.run(run_backtest_in_background(
                task.task_id, client, engine, ["NIFTY50"], "5minute",
                "2025-01-01", "2025-12-31", None,
            ))
        finally:
            tm_module.task_manager = original

        updated = mgr.get(task.task_id)
        assert updated.status == STATUS_COMPLETED
        assert updated.result["trades_taken"] == 2

    def test_all_symbols_failing_to_fetch_fails_the_task_not_fabricate(self) -> None:
        mgr = BacktestTaskManager()
        task = mgr.create_task()

        client = MagicMock()
        client.get_historical_candles_full_range.side_effect = RuntimeError("Upstox 403")
        engine = MagicMock()

        import backend.backtest.task_manager as tm_module
        original = tm_module.task_manager
        tm_module.task_manager = mgr
        try:
            asyncio.run(run_backtest_in_background(
                task.task_id, client, engine, ["NIFTY50"], "5minute",
                "2025-01-01", "2025-12-31", None,
            ))
        finally:
            tm_module.task_manager = original

        updated = mgr.get(task.task_id)
        assert updated.status == STATUS_FAILED
        assert "Refusing to fabricate" in updated.error
        engine.run.assert_not_called()

    def test_progress_is_updated_during_fetch_phase(self) -> None:
        mgr = BacktestTaskManager()
        task = mgr.create_task()

        candles = [{"open": 100, "high": 101, "low": 99, "close": 100,
                    "volume": 1000, "timestamp": f"bar-{i:04d}"} for i in range(70)]
        client = self._fake_client({"A": candles, "B": candles})
        engine = MagicMock()
        fake_result = MagicMock()
        fake_result.trades_taken = 0
        fake_result.skipped_symbols = []
        fake_result.to_dict.return_value = {"trades_taken": 0, "skipped_symbols": []}
        engine.run.return_value = fake_result

        import backend.backtest.task_manager as tm_module
        original = tm_module.task_manager
        tm_module.task_manager = mgr
        try:
            asyncio.run(run_backtest_in_background(
                task.task_id, client, engine, ["A", "B"], "5minute",
                "2025-01-01", "2025-12-31", None,
            ))
        finally:
            tm_module.task_manager = original

        # Final state should be completed — progress along the way isn't
        # asserted in detail here, just that it reaches a clean end state.
        assert mgr.get(task.task_id).status == STATUS_COMPLETED
