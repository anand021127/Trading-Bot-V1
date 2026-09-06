"""Regression tests for Backtest Task Lifecycle and Completion State Handling.

Verifies:
1. Lifecycle transitions: QUEUED -> RUNNING -> COMPLETED / FAILED / CANCELLED.
2. 50K+ candle scenario: simulation finishes and transitions strictly to COMPLETED, never hanging in RUNNING.
3. Invariant enforcement: processed_bars < expected_bars triggers FAILED, not COMPLETED.
4. Status dictionary consistency: result_ready, progress_percent == 100.0, current_phase == COMPLETED.
5. Task retention: active jobs are never evicted prematurely.
6. Post-completion terminal state immutability: late progress updates cannot revert terminal status.
7. End-to-end background runner execution with mocked engine and client.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import MagicMock, patch

from backend.backtest.task_manager import (
    BacktestTask,
    BacktestTaskManager,
    STATUS_QUEUED,
    STATUS_FETCHING_DATA,
    STATUS_RUNNING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    run_backtest_in_background,
)


class TestBacktestLifecycle(unittest.TestCase):

    def setUp(self):
        self.mgr = BacktestTaskManager()

    def test_standard_completion_lifecycle(self):
        """Test QUEUED -> RUNNING -> COMPLETED lifecycle."""
        task = self.mgr.create_task(
            symbols=["NIFTY50", "BANKNIFTY"],
            start_date="2026-01-01",
            end_date="2026-06-09",
            interval="5minute",
        )
        self.assertEqual(task.status, STATUS_QUEUED)
        self.assertFalse(task.to_status_dict()["result_ready"])

        # Phase 1: Data fetching
        self.mgr.update_progress(
            task.task_id,
            {"phase": "fetching_data", "symbols_fetched": 1, "total_symbols": 2},
            status=STATUS_FETCHING_DATA,
        )
        self.assertEqual(self.mgr.get(task.task_id).status, STATUS_FETCHING_DATA)

        # Phase 2: Processing bars
        self.mgr.update_progress(
            task.task_id,
            {"phase": "processing", "bar_index": 25000, "total_bars": 50400},
            status=STATUS_RUNNING,
        )
        status_dict = self.mgr.get(task.task_id).to_status_dict()
        self.assertEqual(status_dict["status"], STATUS_RUNNING)
        self.assertFalse(status_dict["result_ready"])

        # Phase 3: Completion
        result_payload = {
            "total_candles_scanned": 50400,
            "trades_taken": 42,
            "trades": [{"pnl": 1500}],
            "net_profit": 63000.0,
            "trade_log": [{"pnl": 1500}],
        }
        self.mgr.complete(
            task.task_id,
            result=result_payload,
            expected_bars=50400,
            processed_bars=50400,
        )

        completed_task = self.mgr.get(task.task_id)
        self.assertEqual(completed_task.status, STATUS_COMPLETED)
        self.assertEqual(completed_task.current_phase, "COMPLETED")
        self.assertEqual(completed_task.progress_percent, 100.0)
        self.assertIsNotNone(completed_task.completed_at)
        self.assertIsNotNone(completed_task.completed_time)

        final_status = completed_task.to_status_dict()
        self.assertEqual(final_status["status"], STATUS_COMPLETED)
        self.assertEqual(final_status["current_phase"], "COMPLETED")
        self.assertEqual(final_status["progress_percent"], 100.0)
        self.assertTrue(final_status["result_ready"])
        self.assertEqual(final_status["trades_taken"], 42)
        self.assertEqual(final_status["candles_processed"], 50400)
        self.assertIsNone(self.mgr.get_active_task())
        self.assertEqual(self.mgr.get_latest_task().task_id, task.task_id)

    def test_50k_candle_backtest_never_hangs_running(self):
        """Simulate 50,400 candles processed across multiple indices.
        
        Verifies that after engine completes, the task transitions out of RUNNING,
        progress reaches 100%, and results are fully retrievable.
        """
        task = self.mgr.create_task(
            symbols=["NIFTY50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"],
            start_date="2026-01-01",
            end_date="2026-06-09",
            interval="5minute",
        )
        total_candles = 50400

        # Simulate progressive updates up to the last candle
        for bar in [10000, 25000, 50000, 50400]:
            self.mgr.update_progress(
                task.task_id,
                {"phase": "processing", "bar_index": bar, "total_bars": total_candles},
                status=STATUS_RUNNING,
            )

        # Before complete(), status is RUNNING
        self.assertEqual(self.mgr.get(task.task_id).status, STATUS_RUNNING)

        # Simulation finishes and finalizes
        mock_result = {
            "total_candles_scanned": total_candles,
            "trades_taken": 128,
            "net_profit": 245000.0,
            "win_rate": 62.5,
            "trade_log": [{"trade_id": i, "net_pnl": 100} for i in range(128)],
        }
        self.mgr.complete(
            task.task_id,
            result=mock_result,
            expected_bars=total_candles,
            processed_bars=total_candles,
        )

        task_after = self.mgr.get(task.task_id)
        # CRITICAL ASSERTION: status MUST NOT be RUNNING
        self.assertNotEqual(task_after.status, STATUS_RUNNING)
        self.assertEqual(task_after.status, STATUS_COMPLETED)
        self.assertEqual(task_after.current_phase, "COMPLETED")
        self.assertEqual(task_after.progress_percent, 100.0)

        # Result is ready and retrievable
        self.assertIsNotNone(task_after.result)
        self.assertEqual(task_after.result["total_candles_scanned"], 50400)
        self.assertEqual(task_after.result["trades_taken"], 128)

        status_dict = task_after.to_status_dict()
        self.assertTrue(status_dict["result_ready"])
        self.assertEqual(status_dict["status"], "COMPLETED")

    def test_post_completion_terminal_immutability(self):
        """Late or out-of-order progress updates must NOT revert completed task back to RUNNING."""
        task = self.mgr.create_task(symbols=["NIFTY50"])
        self.mgr.complete(
            task.task_id,
            result={"total_candles_scanned": 1000, "trades_taken": 5},
            expected_bars=1000,
            processed_bars=1000,
        )
        self.assertEqual(task.status, STATUS_COMPLETED)

        # Attempt to inject a late progress update
        self.mgr.update_progress(
            task.task_id,
            {"phase": "processing", "bar_index": 500, "total_bars": 1000},
            status=STATUS_RUNNING,
        )

        # Status must remain strictly COMPLETED
        self.assertEqual(task.status, STATUS_COMPLETED)
        self.assertEqual(task.current_phase, "COMPLETED")
        self.assertEqual(task.progress_percent, 100.0)

    def test_incomplete_bars_triggers_failed_invariant(self):
        """If processed_bars < expected_bars, task must fail with BACKTEST_INCOMPLETE."""
        task = self.mgr.create_task(symbols=["NIFTY50"])
        self.mgr.complete(
            task.task_id,
            result={"total_candles_scanned": 40000},
            expected_bars=50400,
            processed_bars=40000,
        )
        self.assertEqual(task.status, STATUS_FAILED)
        self.assertEqual(task.current_phase, "FAILED")
        self.assertIn("Backtest incomplete", task.error)
        self.assertIsNone(task.result)

    def test_cancellation_lifecycle(self):
        """Cancellation properly sets CANCELLED status and prevents further updates."""
        task = self.mgr.create_task(symbols=["BANKNIFTY"])
        self.mgr.update_progress(task.task_id, {"phase": "processing"}, status=STATUS_RUNNING)

        cancelled_task = self.mgr.cancel(task.task_id, reason="User clicked Cancel")
        self.assertEqual(cancelled_task.status, STATUS_CANCELLED)
        self.assertEqual(cancelled_task.current_phase, "CANCELLED")

        # Further updates must be rejected
        self.mgr.update_progress(task.task_id, {"phase": "processing"}, status=STATUS_RUNNING)
        self.assertEqual(task.status, STATUS_CANCELLED)

    def test_active_tasks_never_evicted(self):
        """Running and active tasks must NEVER be evicted by background sweep."""
        task = self.mgr.create_task(symbols=["NIFTY50"])
        task.updated_at = 0  # Artificially age the task
        task.status = STATUS_RUNNING

        self.mgr._evict_old_tasks()
        self.assertIn(task.task_id, self.mgr._tasks)
        self.assertIsNotNone(self.mgr.get_active_task())

    def test_background_runner_end_to_end_mock(self):
        """Verify run_backtest_in_background handles lifecycle, phases, and completion."""
        async def run_test():
            task = self.mgr.create_task(symbols=["NIFTY50"], start_date="2026-01-01", end_date="2026-01-05", interval="5minute")
            
            mock_client = MagicMock()
            mock_engine = MagicMock()

            # Mock backtest result
            mock_result = MagicMock()
            mock_result.total_candles_scanned = 100
            mock_result.trades_taken = 3
            mock_result.skipped_symbols = []
            mock_result.to_dict.return_value = {
                "total_candles_scanned": 100,
                "trades_taken": 3,
                "net_profit": 5000.0,
                "trades": [{"pnl": 2000}],
            }
            mock_engine.run.return_value = mock_result

            # Mock load_dataset_safe to return candles
            fake_candles = [
                {
                    "timestamp": f"2024-01-02T09:{15 + (i % 45):02d}:00+05:30",
                    "open": 21700.0,
                    "high": 21750.0,
                    "low": 21650.0,
                    "close": 21720.0,
                    "volume": 1000,
                }
                for i in range(100)
            ]

            mock_client.get_historical_candles_full_range.return_value = fake_candles

            with patch("backend.backtest.task_manager.task_manager", self.mgr), \
                 patch("backend.backtest.historical_data_io.load_dataset_safe", return_value=fake_candles), \
                 patch("backend.broker.upstox_expired_options.UpstoxExpiredOptionsClient"), \
                 patch("backend.backtest.options_data_layer.HistoricalOptionsDataLoader"):
                await run_backtest_in_background(
                    task_id=task.task_id,
                    client=mock_client,
                    engine=mock_engine,
                    symbols=["NIFTY50"],
                    interval="5minute",
                    start_date="2024-01-01",
                    end_date="2024-01-05",
                    strategy_names=["OPTION_PREMIUM"],
                )

            final_task = self.mgr.get(task.task_id)
            self.assertEqual(final_task.status, STATUS_COMPLETED)
            self.assertEqual(final_task.current_phase, "COMPLETED")
            self.assertEqual(final_task.progress_percent, 100.0)
            self.assertIsNotNone(final_task.result)
            self.assertEqual(final_task.result["trades_taken"], 3)
            self.assertTrue(final_task.to_status_dict()["result_ready"])

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
