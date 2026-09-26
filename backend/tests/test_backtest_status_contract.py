"""Backtest status API-contract tests (PHASE 4 gap #4).

Pins the ONE authoritative enum: the exact wire values, the terminal/active
classification, lowercase legacy normalization, the FRONTEND union parity
(the TS type in frontend/src/types/index.ts must list exactly the same
strings), and the router contract: durable rows are served when the
in-memory mirror is gone (restart), and INTERRUPTED_BY_RESTART is never
reported COMPLETED.
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.backtest import status as bt_status
from backend.backtest.job_store import BacktestJobStore


class TestEnumValues(unittest.TestCase):
    def test_exact_wire_values(self):
        self.assertEqual(
            sorted(bt_status.ALL_STATUSES),
            sorted([
                "QUEUED", "FETCHING_DATA", "RESOLVING_CONTRACTS", "RUNNING",
                "FINALIZING", "COMPLETED", "FAILED", "CANCELLED",
                "INTERRUPTED_BY_RESTART",
            ]),
        )

    def test_terminal_classification(self):
        for s in ("COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED_BY_RESTART"):
            self.assertTrue(bt_status.is_terminal(s), s)
        for s in ("QUEUED", "FETCHING_DATA", "RESOLVING_CONTRACTS", "RUNNING", "FINALIZING"):
            self.assertFalse(bt_status.is_terminal(s), s)
        # terminal + active partition the whole set
        self.assertEqual(bt_status.TERMINAL_STATUSES | bt_status.ACTIVE_STATUSES, bt_status.ALL_STATUSES)
        self.assertFalse(bt_status.TERMINAL_STATUSES & bt_status.ACTIVE_STATUSES)

    def test_normalize_legacy_lowercase(self):
        self.assertEqual(bt_status.normalize_status("completed"), "COMPLETED")
        self.assertEqual(bt_status.normalize_status("running"), "RUNNING")
        self.assertEqual(bt_status.normalize_status("FAILED"), "FAILED")
        self.assertEqual(bt_status.normalize_status(""), "")

    def test_task_manager_reexports_match(self):
        from backend.backtest import task_manager as tm
        self.assertEqual(tm.STATUS_COMPLETED, "COMPLETED")
        self.assertEqual(tm.STATUS_INTERRUPTED_BY_RESTART, "INTERRUPTED_BY_RESTART")
        self.assertEqual(tm.STATUS_RESOLVING_CONTRACTS, "RESOLVING_CONTRACTS")
        self.assertEqual(tm.STATUS_FINALIZING, "FINALIZING")

    def test_frontend_union_parity(self):
        """The TS BacktestStatus union must list exactly the backend values."""
        ts_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "frontend", "src", "types", "index.ts",
        )
        if not os.path.exists(ts_path):
            self.skipTest("frontend source not present in this checkout")
        src = open(ts_path, encoding="utf-8").read()
        m = re.search(r"export type BacktestStatus\s*=\s*(.*?)export const", src, re.S)
        self.assertIsNotNone(m, "BacktestStatus union not found in types/index.ts")
        values = set(re.findall(r"'([A-Z_]+)'", m.group(1)))
        self.assertEqual(values, set(bt_status.ALL_STATUSES))


def _app() -> FastAPI:
    from backend.api.routers.backtest import router
    app = FastAPI()
    app.include_router(router, prefix="/api/backtest")
    return app


class TestRouterServesDurableRows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = BacktestJobStore(os.path.join(self.tmp, "jobs.db"))
        self.addCleanup(self.store.close)
        # Point the router's job_store singleton at the temp DB.
        self.patcher = patch("backend.api.routers.backtest.job_store", self.store)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.client = TestClient(_app())

    def test_status_endpoint_serves_row_after_memory_eviction(self):
        job = self.store.create_job(
            strategies=["V8_D_PULLBACK_ATM"], symbols=["NIFTY50"],
            start_date="2024-10-01", end_date="2024-10-31",
            interval="5minute", capital=100000.0,
        )
        self.store.update_status(job["job_id"], "RUNNING")
        self.store.set_result(job["job_id"], {"net_profit": 5.0, "trades_taken": 1})
        # No in-memory task exists (fresh process) — the durable row answers.
        r = self.client.get(f"/api/backtest/jobs/{job['job_id']}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "COMPLETED")
        self.assertTrue(body["result_ready"])
        self.assertEqual(body["trades_taken"], 1)
        self.assertTrue(body.get("durable"))

    def test_interrupted_job_reported_honestly(self):
        job = self.store.create_job(
            strategies=["V8_D_PULLBACK_ATM"], symbols=["NIFTY50"],
            start_date="2024-10-01", end_date="2024-10-31",
            interval="5minute", capital=100000.0,
        )
        self.store.update_status(job["job_id"], "RUNNING")
        self.store.recover_interrupted()
        r = self.client.get(f"/api/backtest/jobs/{job['job_id']}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "INTERRUPTED_BY_RESTART")

        r2 = self.client.get(f"/api/backtest/result/{job['job_id']}")
        self.assertEqual(r2.status_code, 200)
        body = r2.json()
        self.assertEqual(body["status"], "INTERRUPTED_BY_RESTART")
        self.assertIn("interrupted", body["message"].lower())
        self.assertNotIn("result_ready", body)

    def test_failed_result_still_surfaced_after_restart(self):
        job = self.store.create_job(
            strategies=["V8_D_PULLBACK_ATM"], symbols=["NIFTY50"],
            start_date="2024-10-01", end_date="2024-10-31",
            interval="5minute", capital=100000.0,
        )
        self.store.update_status(job["job_id"], "RUNNING")
        self.store.set_error(job["job_id"], {"code": "DATA_UNAVAILABLE", "message": "no candles for NIFTY50"})
        r = self.client.get(f"/api/backtest/jobs/{job['job_id']}")
        self.assertEqual(r.json()["status"], "FAILED")
        r2 = self.client.get(f"/api/backtest/result/{job['job_id']}")
        self.assertEqual(r2.status_code, 502)
        self.assertIn("no candles", r2.json()["detail"])

    def test_cancelled_result_after_restart(self):
        job = self.store.create_job(
            strategies=["V8_D_PULLBACK_ATM"], symbols=["NIFTY50"],
            start_date="2024-10-01", end_date="2024-10-31",
            interval="5minute", capital=100000.0,
        )
        self.store.update_status(job["job_id"], "RUNNING")
        self.store.request_cancel(job["job_id"])
        self.store.update_status(job["job_id"], "CANCELLED")
        r2 = self.client.get(f"/api/backtest/result/{job['job_id']}")
        self.assertEqual(r2.status_code, 400)
        self.assertIn("cancelled", r2.json()["detail"].lower())

    def test_jobs_active_reports_latest_durable_job(self):
        job = self.store.create_job(
            strategies=["V8_D_PULLBACK_ATM"], symbols=["NIFTY50"],
            start_date="2024-10-01", end_date="2024-10-31",
            interval="5minute", capital=100000.0,
        )
        self.store.set_result(job["job_id"], {"net_profit": 0.0})
        r = self.client.get("/api/backtest/jobs/active")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["active"])
        self.assertEqual(body["job"]["job_id"], job["job_id"])
        self.assertEqual(body["job"]["status"], "COMPLETED")

    def test_recover_endpoint_reports_recovered_ids(self):
        job = self.store.create_job(
            strategies=["V8_D_PULLBACK_ATM"], symbols=["NIFTY50"],
            start_date="2024-10-01", end_date="2024-10-31",
            interval="5minute", capital=100000.0,
        )
        self.store.update_status(job["job_id"], "RUNNING")
        r = self.client.post("/api/backtest/jobs/recover")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn(job["job_id"], body["recovered"])
        self.assertEqual(self.store.get(job["job_id"])["status"], "INTERRUPTED_BY_RESTART")


if __name__ == "__main__":
    unittest.main()
