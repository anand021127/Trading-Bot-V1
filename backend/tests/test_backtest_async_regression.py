"""Regression tests for the async backtest architecture.

Locks in the fix for the production "timeout of 30000ms exceeded" bug:

1. POST /api/backtest/run returns a job id IMMEDIATELY (202) — the HTTP
   request must not stay open until the backtest finishes.
2. A long-running job stays pollable with SHORT requests and must NEVER
   be reported as a timeout while it is still running.
3. Cancellation via POST /api/backtest/status/{id}/cancel works.
4. Duplicate submissions (double-click) are rejected with 409.
5. Real backend failure reasons surface in job status (no generic
   timeout text).
6. Historical API timeout retries don't abort the whole job
   (fetch_full_range_with_retry).
"""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.backtest.historical_fetch import fetch_full_range_with_retry
from backend.backtest.task_manager import BacktestTaskManager


class _SlowFetchTimeout(Exception):
    status_code = 408


def _build_router_app():
    """Minimal FastAPI app mounting ONLY the backtest router — avoids
    main.py's DatabaseManager side effects while testing the real
    routes."""
    from backend.api.routers.backtest import router

    app = FastAPI()
    app.include_router(router, prefix="/api/backtest")
    return app


class TestRunBacktestReturnsImmediately(unittest.TestCase):
    def setUp(self):
        from backend.backtest.task_manager import task_manager as singleton
        singleton._tasks.clear()
        self.addCleanup(singleton._tasks.clear)

    def test_run_returns_202_with_job_id_fast(self):
        """POST /run must return a job id quickly — NOT wait for the
        backtest to finish. Uses a deliberately SLOW background client so
        that, if the route were still synchronous, the request could not
        complete within the test timeout."""
        app = _build_router_app()
        client = TestClient(app)

        captured = {}

        def _slow_background(task_id, *args, **kwargs):
            captured["started"] = time.monotonic()
            async def _runner():
                await asyncio.sleep(30)  # far longer than the request window
            return asyncio.ensure_future(_runner())

        slow_client = MagicMock()
        slow_client.get_historical_candles_full_range = MagicMock(
            side_effect=lambda *a, **k: time.sleep(30)
        )

        started = time.monotonic()
        # NOTE: the router imports run_backtest_in_background directly, so
        # the patch target must be the ROUTER's binding, not the
        # task_manager module attribute.
        with patch(
            "backend.broker.token_resolver.resolve_upstox_token",
            return_value="test-token-abc",
        ), patch(
            "backend.broker.upstox_client.UpstoxClient",
            return_value=slow_client,
        ), patch(
            "backend.api.routers.backtest.run_backtest_in_background",
            side_effect=_slow_background,
        ):
            resp = client.post("/api/backtest/run", json={
                "start_date": "2026-07-01", "end_date": "2026-09-25",
                "capital": 20000, "symbols": ["NIFTY50"],
                "interval": "5minute", "strategies": ["V8_D_PULLBACK_ATM"],
            })
        elapsed = time.monotonic() - started

        self.assertEqual(resp.status_code, 202, resp.text)
        body = resp.json()
        self.assertIn("task_id", body)
        self.assertTrue(body["task_id"])
        self.assertIn("job_id", body)
        # The request itself must be fast — well under any axios window.
        self.assertLess(elapsed, 5.0, f"POST /run took {elapsed:.1f}s — request is not returning immediately")
        self.assertLess(elapsed, 25.0)

    def test_double_click_creates_single_job_409_on_duplicate(self):
        """Double-clicking Run Backtest must not create two jobs."""
        app = _build_router_app()
        client = TestClient(app)

        def _noop_background(task_id, *args, **kwargs):
            async def _runner():
                await asyncio.sleep(60)  # stays "active" during the test
            return asyncio.ensure_future(_runner())

        with patch("backend.broker.token_resolver.resolve_upstox_token",
                   return_value="test-token-abc"), patch(
            "backend.broker.upstox_client.UpstoxClient"), patch(
            "backend.api.routers.backtest.run_backtest_in_background",
            side_effect=_noop_background,
        ):
            first = client.post("/api/backtest/run", json={
                "symbols": ["NIFTY50"], "interval": "5minute",
                "strategies": ["V8_D_PULLBACK_ATM"],
                "start_date": "2026-07-01", "end_date": "2026-09-25",
            })
            self.assertEqual(first.status_code, 202, first.text)
            second = client.post("/api/backtest/run", json={
                "symbols": ["NIFTY50"], "interval": "5minute",
                "strategies": ["V8_D_PULLBACK_ATM"],
                "start_date": "2026-07-01", "end_date": "2026-09-25",
            })
        self.assertEqual(second.status_code, 409, second.text)
        detail = second.json()["detail"]
        self.assertIn("already running", detail["message"])
        self.assertEqual(detail["active_job_id"], first.json()["task_id"])

    def test_no_token_fails_fast_with_clear_reason(self):
        app = _build_router_app()
        client = TestClient(app)
        with patch("backend.broker.token_resolver.resolve_upstox_token",
                   return_value=""):
            resp = client.post("/api/backtest/run", json={
                "symbols": ["NIFTY50"], "strategies": ["V8_D_PULLBACK_ATM"]})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("token", resp.json()["detail"].lower())


class TestPollingWhileRunning(unittest.TestCase):
    def setUp(self):
        self.mgr = BacktestTaskManager()

    # (tests use a private manager instance; the singleton stays untouched)

    def test_long_running_job_polls_short_and_never_times_out(self):
        """A job still RUNNING must report running status on short polls —
        never a timeout. Simulates 3+ minutes of work with 10 quick polls."""
        task = self.mgr.create_task(
            symbols=["NIFTY50"], start_date="2026-07-01",
            end_date="2026-09-25", interval="5minute")
        self.mgr.update_progress(task.task_id,
                                 {"phase": "fetching_data", "symbols_fetched": 0,
                                  "total_symbols": 1},
                                 status="FETCHING_DATA")
        self.mgr.update_progress(task.task_id,
                                 {"phase": "processing", "symbol": "NIFTY50",
                                  "bar_index": 500, "total_bars": 20000,
                                  "progress_percent": 32.5},
                                 status="RUNNING")

        for i in range(10):
            d = self.mgr.get(task.task_id).to_status_dict()
            self.assertEqual(d["status"], "RUNNING")
            self.assertGreater(d["progress_percent"], 0)
            self.assertIn("elapsed_seconds", d)
            self.assertLess(d["elapsed_seconds"], 600)

    def test_progress_percent_and_symbol_survive_serialization(self):
        task = self.mgr.create_task(symbols=["NIFTY50", "BANKNIFTY"])
        self.mgr.update_progress(task.task_id,
                                 {"phase": "processing", "symbol": "BANKNIFTY",
                                  "bar_index": 1200, "total_bars": 4000},
                                 status="RUNNING")
        d = self.mgr.get(task.task_id).to_status_dict()
        self.assertEqual(d["current_symbol"], "BANKNIFTY")
        self.assertGreater(d["progress_percent"], 30)
        self.assertEqual(d["total_symbols"], 2)


class TestCancellation(unittest.TestCase):
    def setUp(self):
        self.mgr = BacktestTaskManager()

    def test_cancel_running_job(self):
        task = self.mgr.create_task(symbols=["NIFTY50"])
        self.mgr.update_progress(task.task_id, {"phase": "processing"},
                                 status="RUNNING")
        self.assertTrue(task.cancel(reason="Cancelled by user"))
        self.assertEqual(task.status, "CANCELLED")
        self.assertIn("cancel", (task.error or "").lower())

    def test_cancel_endpoint_returns_status(self):
        from backend.api.routers.backtest import _cancel_job
        from backend.backtest.task_manager import task_manager as singleton
        task = singleton.create_task(symbols=["NIFTY50"])
        self.addCleanup(singleton._tasks.pop, task.task_id, None)
        d = _cancel_job(task.task_id)
        self.assertTrue(d["cancelled"])
        self.assertEqual(d["status"], "CANCELLED")

    def test_cancel_unknown_job_404(self):
        from backend.api.routers.backtest import _cancel_job
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            _cancel_job("nonexistent-job-id")
        self.assertEqual(ctx.exception.status_code, 404)


class TestHonestFailureReasons(unittest.TestCase):
    def test_failed_task_reports_real_backend_reason(self):
        self.mgr = BacktestTaskManager()
        task = self.mgr.create_task(symbols=["NIFTY50"])
        self.mgr.fail(task.task_id, error={
            "code": "DATA_UNAVAILABLE",
            "message": "DATA_UNAVAILABLE: Failed to load complete historical "
                       "candles for requested symbols ['NIFTY50']. "
                       "Refusing to fabricate or mark partial run as completed.",
        })
        d = self.mgr.get(task.task_id).to_status_dict()
        self.assertEqual(d["status"], "FAILED")
        self.assertIn("DATA_UNAVAILABLE", d["error"])
        self.assertNotIn("timeout of 30000ms", (d["error"] or "").lower())

    def test_incomplete_coverage_is_reported_not_hidden(self):
        self.mgr = BacktestTaskManager()
        task = self.mgr.create_task(symbols=["NIFTY50"])
        self.mgr.complete(
            task.task_id,
            result={"total_candles_scanned": 10},
            expected_bars=100,
            processed_bars=10,
        )
        self.assertEqual(self.mgr.get(task.task_id).status, "FAILED")
        d = self.mgr.get(task.task_id).to_status_dict()
        self.assertIn("processed 10 of 100", d["error"])


class TestHistoricalFetchRetry(unittest.TestCase):
    def test_transient_timeout_is_retried_not_fatal(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise _SlowFetchTimeout("Timeout for https://api.upstox.com")
            return [{"close": 1}]

        out = fetch_full_range_with_retry(flaky, symbol="NIFTY50",
                                          sleep=lambda s: None)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(out, [{"close": 1}])

    def test_exhausted_retries_raise_last_real_error(self):
        calls = {"n": 0}

        def always_timeout():
            calls["n"] += 1
            raise _SlowFetchTimeout("Timeout for https://api.upstox.com")

        with self.assertRaises(_SlowFetchTimeout):
            fetch_full_range_with_retry(always_timeout, symbol="NIFTY50",
                                        max_attempts=3, sleep=lambda s: None)
        self.assertEqual(calls["n"], 3)

    def test_auth_error_not_retried(self):
        calls = {"n": 0}

        def auth_fail():
            calls["n"] += 1
            e = Exception("Token invalid or expired")
            e.status_code = 401
            raise e

        with self.assertRaises(Exception):
            fetch_full_range_with_retry(auth_fail, symbol="NIFTY50",
                                        max_attempts=3, sleep=lambda s: None)
        self.assertEqual(calls["n"], 1, "401 must not be retried")

    def test_rate_limit_retried(self):
        calls = {"n": 0}

        def rate_limited():
            calls["n"] += 1
            if calls["n"] < 2:
                e = Exception("Rate limit hit.")
                e.status_code = 429
                raise e
            return []

        fetch_full_range_with_retry(rate_limited, symbol="NIFTY50",
                                    sleep=lambda s: None)
        self.assertEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
