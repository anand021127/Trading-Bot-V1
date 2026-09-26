"""Durable backtest job store tests (PHASE 4 gap #2).

Covers: idempotent updates, restart recovery (RUNNING → INTERRUPTED_BY_RESTART,
never COMPLETED), retrievable completed/failed/cancelled results after a
simulated restart, DB-enforced single-active-job rule, WAL mode, and progress
persistence integrity.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from backend.backtest.job_store import (
    BacktestJobStore,
    DuplicateActiveJobError,
)
from backend.backtest.status import INTERRUPTED_BY_RESTART


def _mk_store(tmp: str) -> BacktestJobStore:
    return BacktestJobStore(os.path.join(tmp, "jobs.db"))


def _mk_job(store: BacktestJobStore, tag: str = "NIFTY50"):
    return store.create_job(
        strategies=["V8_D_PULLBACK_ATM"],
        symbols=[tag],
        start_date="2024-10-01",
        end_date="2024-10-31",
        interval="5minute",
        capital=100000.0,
    )


class TestJobStoreBasics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = _mk_store(self.tmp)
        self.addCleanup(self.store.close)

    def test_create_and_get_roundtrip(self):
        job = _mk_job(self.store)
        row = self.store.get(job["job_id"])
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "QUEUED")
        self.assertEqual(row["strategies"], ["V8_D_PULLBACK_ATM"])
        self.assertEqual(row["symbols"], ["NIFTY50"])
        self.assertEqual(row["capital"], 100000.0)
        self.assertEqual(row["interval"], "5minute")
        self.assertFalse(row["cancel_requested"])

    def test_status_transitions_persist(self):
        job = _mk_job(self.store)
        jid = job["job_id"]
        for status in ("FETCHING_DATA", "RESOLVING_CONTRACTS", "RUNNING", "FINALIZING"):
            self.assertTrue(self.store.update_status(jid, status))
            self.assertEqual(self.store.get(jid)["status"], status)
        self.assertIsNotNone(self.store.get(jid)["started_at"])

    def test_progress_upsert_is_idempotent(self):
        job = _mk_job(self.store)
        jid = job["job_id"]
        prog = {"phase": "processing", "processed_bars": 40, "total_bars": 100, "symbol": "NIFTY50"}
        for _ in range(5):
            self.store.update_progress(jid, prog)
        row = self.store.get(jid)
        self.assertEqual(row["processed_bars"], 40)
        self.assertEqual(row["total_bars"], 100)
        # progress JSON is exactly what was last written
        self.assertEqual(row["progress"]["processed_bars"], 40)

    def test_result_and_error_persist(self):
        job = _mk_job(self.store)
        jid = job["job_id"]
        self.store.set_result(jid, {"net_profit": -12.5, "trades_taken": 3})
        row = self.store.get(jid)
        self.assertEqual(row["status"], "COMPLETED")
        self.assertEqual(row["result"]["net_profit"], -12.5)

        job2 = _mk_job(self.store)
        self.store.update_status(job2["job_id"], "RUNNING")
        self.store.set_error(job2["job_id"], {"code": "DATA_UNAVAILABLE", "message": "no candles"})
        row2 = self.store.get(job2["job_id"])
        self.assertEqual(row2["status"], "FAILED")
        self.assertEqual(row2["error"], "no candles")
        self.assertEqual(row2["error_details"]["code"], "DATA_UNAVAILABLE")


class TestRestartRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "jobs.db")
        self.store = BacktestJobStore(self.db)
        self.addCleanup(self.store.close)

    def _reopen(self) -> BacktestJobStore:
        """Simulate a backend restart: closed process, same DB file."""
        self.store.close()
        return BacktestJobStore(self.db)

    def test_running_job_becomes_interrupted_on_restart(self):
        job = _mk_job(self.store)
        self.store.update_status(job["job_id"], "RUNNING")
        self.store.update_progress(job["job_id"], {"processed_bars": 500, "total_bars": 1000})
        fresh = self._reopen()
        recovered = fresh.recover_interrupted()
        self.assertIn(job["job_id"], recovered)
        row = fresh.get(job["job_id"])
        self.assertEqual(row["status"], INTERRUPTED_BY_RESTART)
        self.assertNotEqual(row["status"], "COMPLETED")
        # progress survives so the operator sees how far it got
        self.assertEqual(row["processed_bars"], 500)

    def test_every_active_state_recovered(self):
        """Each active state (one at a time — the DB enforces a single active
        job) must be recovered to INTERRUPTED_BY_RESTART by the next store."""
        for st in ("QUEUED", "FETCHING_DATA", "RESOLVING_CONTRACTS", "RUNNING", "FINALIZING"):
            job = _mk_job(self.store)
            self.store.update_status(job["job_id"], st)
            fresh = self._reopen()
            recovered = fresh.recover_interrupted()
            self.assertIn(job["job_id"], recovered, st)
            row = fresh.get(job["job_id"])
            self.assertEqual(row["status"], INTERRUPTED_BY_RESTART, st)
            self.store = fresh  # continue from the restarted store

    def test_terminal_jobs_untouched_by_recovery(self):
        done = _mk_job(self.store)
        self.store.set_result(done["job_id"], {"net_profit": 1.0})
        failed = _mk_job(self.store)
        self.store.update_status(failed["job_id"], "RUNNING")
        self.store.set_error(failed["job_id"], "boom")
        cancelled = _mk_job(self.store)
        self.store.update_status(cancelled["job_id"], "RUNNING")
        self.store.request_cancel(cancelled["job_id"])
        self.store.update_status(cancelled["job_id"], "CANCELLED")

        fresh = self._reopen()
        recovered = fresh.recover_interrupted()
        self.assertEqual(recovered, [])
        self.assertEqual(fresh.get(done["job_id"])["status"], "COMPLETED")
        self.assertEqual(fresh.get(failed["job_id"])["status"], "FAILED")
        self.assertEqual(fresh.get(cancelled["job_id"])["status"], "CANCELLED")

    def test_results_retrievable_after_restart(self):
        done = _mk_job(self.store)
        self.store.set_result(done["job_id"], {"net_profit": 99.5, "trade_log": [{"x": 1}]})
        failed = _mk_job(self.store)
        self.store.update_status(failed["job_id"], "RUNNING")
        self.store.set_error(failed["job_id"], {"code": "BACKTEST_INCOMPLETE", "message": "bar gap"})
        cancelled = _mk_job(self.store)
        self.store.update_status(cancelled["job_id"], "RUNNING")
        self.store.update_status(cancelled["job_id"], "CANCELLED")

        fresh = self._reopen()
        self.assertEqual(fresh.get(done["job_id"])["result"]["net_profit"], 99.5)
        self.assertEqual(fresh.get(failed["job_id"])["error"], "bar gap")
        self.assertEqual(fresh.get(cancelled["job_id"])["status"], "CANCELLED")

    def test_single_active_job_enforced_across_restart(self):
        _mk_job(self.store)  # stays QUEUED (active)
        fresh = self._reopen()
        with self.assertRaises(DuplicateActiveJobError):
            _mk_job(fresh)


class TestStoreHygiene(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "jobs.db")
        self.store = BacktestJobStore(self.db)
        self.addCleanup(self.store.close)

    def test_wal_mode_active(self):
        mode = self.store._connect().execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(str(mode).lower(), "wal")

    def test_schema_has_required_columns(self):
        cols = {r[1] for r in self.store._connect().execute("PRAGMA table_info(backtest_jobs)")}
        required = {
            "job_id", "status", "phase", "strategies", "symbols", "start_date",
            "end_date", "interval", "capital", "created_at", "started_at",
            "updated_at", "completed_at", "progress", "processed_bars",
            "total_bars", "bars_per_second", "eta_seconds", "current_symbol",
            "current_timestamp", "result", "error", "error_details", "cancel_requested",
        }
        missing = required - cols
        self.assertEqual(missing, set())

    def test_invalid_status_rejected(self):
        job = _mk_job(self.store)
        with self.assertRaises(ValueError):
            self.store.update_status(job["job_id"], "MADE_UP_STATE")

    def test_close_is_idempotent(self):
        self.store.close()
        self.store.close()  # must not raise


if __name__ == "__main__":
    unittest.main()
