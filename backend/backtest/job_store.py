"""Durable SQLite persistence for backtest jobs.

WHY: the in-memory task store lost all job state on backend restart — a job
that was RUNNING died silently with the process, a COMPLETED result vanished,
and the frontend had nothing to recover. This store persists every field the
API contract exposes so that:
  - a completed/failed/cancelled result remains retrievable after restart
  - a job that was RUNNING at process death is marked INTERRUPTED_BY_RESTART
    (never COMPLETED, never left RUNNING) on the next startup
  - duplicate-job protection survives restarts via a UNIQUE active-job index
  - progress writes are idempotent single-row upserts inside transactions

Connection hygiene follows the project's DatabaseManager conventions: WAL,
busy_timeout, synchronous=NORMAL, one connection guarded by a threading.Lock
(check_same_thread=False so API threads + asyncio callers share it safely).

The store owns ONLY persistence + restart recovery. Lifecycle transition
rules (what may move where) live in task_manager; status string values come
from backend/backtest/status.py — the ONE authoritative enum.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from backend.backtest.status import (
    ALL_STATUSES,
    ACTIVE_STATUSES,
    INTERRUPTED_BY_RESTART,
    RUNNING,
    normalize_status,
)

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS backtest_jobs (
    job_id            TEXT PRIMARY KEY,
    status            TEXT NOT NULL,
    phase             TEXT,
    strategies        TEXT,            -- JSON list
    symbols           TEXT,            -- JSON list
    start_date        TEXT,
    end_date          TEXT,
    interval          TEXT,
    capital           REAL,
    created_at        TEXT,            -- UTC ISO
    started_at        TEXT,
    updated_at        TEXT,
    completed_at      TEXT,
    progress          TEXT,            -- JSON dict
    processed_bars    INTEGER,
    total_bars        INTEGER,
    bars_per_second   REAL,
    eta_seconds       REAL,
    current_symbol    TEXT,
    current_timestamp TEXT,
    result            TEXT,            -- JSON full result payload
    error             TEXT,
    error_details     TEXT,            -- JSON dict
    cancel_requested  INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_backtest_jobs_one_active
    ON backtest_jobs ((status IN ('QUEUED','FETCHING_DATA','RESOLVING_CONTRACTS','RUNNING','FINALIZING')))
    WHERE status IN ('QUEUED','FETCHING_DATA','RESOLVING_CONTRACTS','RUNNING','FINALIZING');
"""


class BacktestJobStore:
    """SQLite-backed durable store for backtest jobs."""

    def __init__(self, db_path: str = "data/backtest_jobs.db") -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        if db_path != ":memory:":
            parent = os.path.dirname(db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._init()

    # ── connection / schema ───────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass  # :memory: or read-only — default journal is fine
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._conn = conn
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def _init(self) -> None:
        with self._lock:
            conn = self._connect()
            conn.executescript(_SCHEMA)
            conn.commit()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # ── row mapping ───────────────────────────────────────────────────
    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        def _j(key: str, default: Any) -> Any:
            raw = row[key]
            if not raw:
                return default
            try:
                return json.loads(raw)
            except Exception:
                return default

        return {
            "job_id": row["job_id"],
            "status": row["status"],
            "phase": row["phase"],
            "strategies": _j("strategies", []),
            "symbols": _j("symbols", []),
            "start_date": row["start_date"],
            "end_date": row["end_date"],
            "interval": row["interval"],
            "capital": row["capital"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
            "progress": _j("progress", {}),
            "processed_bars": row["processed_bars"],
            "total_bars": row["total_bars"],
            "bars_per_second": row["bars_per_second"],
            "eta_seconds": row["eta_seconds"],
            "current_symbol": row["current_symbol"],
            "current_timestamp": row["current_timestamp"],
            "result": _j("result", None),
            "error": row["error"],
            "error_details": _j("error_details", None),
            "cancel_requested": bool(row["cancel_requested"]),
        }

    # ── CRUD ──────────────────────────────────────────────────────────
    def create_job(
        self,
        *,
        strategies: List[str],
        symbols: List[str],
        start_date: str,
        end_date: str,
        interval: str,
        capital: Optional[float],
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert a QUEUED job. Raises DuplicateActiveJobError when another
        job is already active (DB-enforced, so it holds across restarts)."""
        job_id = job_id or str(uuid.uuid4())
        now = self._now_iso()
        rec = {
            "job_id": job_id,
            "status": "QUEUED",
            "phase": "QUEUED",
            "strategies": json.dumps(list(strategies)),
            "symbols": json.dumps(list(symbols)),
            "start_date": start_date,
            "end_date": end_date,
            "interval": interval,
            "capital": capital,
            "created_at": now,
            "started_at": None,
            "updated_at": now,
            "completed_at": None,
            "progress": json.dumps({}),
            "processed_bars": 0,
            "total_bars": 0,
            "bars_per_second": None,
            "eta_seconds": None,
            "current_symbol": symbols[0] if symbols else "",
            "current_timestamp": None,
            "result": None,
            "error": None,
            "error_details": None,
            "cancel_requested": 0,
        }
        cols = ", ".join(rec.keys())
        qs = ", ".join("?" for _ in rec)
        try:
            with self._transaction() as conn:
                conn.execute(f"INSERT INTO backtest_jobs ({cols}) VALUES ({qs})", tuple(rec.values()))
        except sqlite3.IntegrityError as exc:
            raise DuplicateActiveJobError(
                "Another backtest job is already active (DB-enforced)."
            ) from exc
        return self.get(job_id)  # type: ignore[return-value]

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connect().execute(
                "SELECT * FROM backtest_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._connect().execute(
                "SELECT * FROM backtest_jobs ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_active(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connect().execute(
                """SELECT * FROM backtest_jobs
                   WHERE status IN ('QUEUED','FETCHING_DATA','RESOLVING_CONTRACTS','RUNNING','FINALIZING')
                   ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_latest(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connect().execute(
                "SELECT * FROM backtest_jobs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def update_status(self, job_id: str, status: str, phase: Optional[str] = None) -> bool:
        status = normalize_status(status)
        if status not in ALL_STATUSES:
            raise ValueError(f"Invalid backtest status: {status!r}")
        sets = ["status=?", "updated_at=?"]
        vals: List[Any] = [status, self._now_iso()]
        if phase is not None:
            sets.append("phase=?")
            vals.append(phase)
        else:
            sets.append("phase=?")
            vals.append(status)
        if status == "RUNNING":
            sets.append("started_at=COALESCE(started_at, ?)")
            vals.append(self._now_iso())
        if status in ("COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED_BY_RESTART"):
            sets.append("completed_at=COALESCE(completed_at, ?)")
            vals.append(self._now_iso())
        vals.append(job_id)
        with self._transaction() as conn:
            cur = conn.execute(
                f"UPDATE backtest_jobs SET {', '.join(sets)} WHERE job_id=?", tuple(vals)
            )
        return cur.rowcount > 0

    def update_progress(self, job_id: str, progress: Dict[str, Any]) -> None:
        """Idempotent progress upsert. Only meaningful within active jobs.

        Computes bars/second + ETA from the row's own started_at timestamp so
        the persisted rate is restart-stable and not derived from wall-clock
        guesses in the caller.
        """
        processed = progress.get("processed_bars", progress.get("bar_index"))
        total = progress.get("total_bars", progress.get("expected_bars"))
        bps = progress.get("bars_per_second")
        eta = progress.get("eta_seconds")
        try:
            p = int(processed) if processed is not None else None
            t = int(total) if total else None
            if p is not None and t and p > 0:
                with self._lock:
                    row = self._connect().execute(
                        "SELECT started_at, created_at FROM backtest_jobs WHERE job_id=?",
                        (job_id,),
                    ).fetchone()
                if row is not None:
                    base = row["started_at"] or row["created_at"]
                    if base:
                        started = datetime.fromisoformat(base)
                        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                        if elapsed > 0.5:
                            bps = bps if bps else round(p / elapsed, 1)
                            eta = eta if eta else round(max(0.0, (t - p) / max(bps, 0.001)), 1)
        except Exception:
            pass
        with self._transaction() as conn:
            conn.execute(
                """UPDATE backtest_jobs SET
                     progress=?,
                     processed_bars=COALESCE(?, processed_bars),
                     total_bars=COALESCE(?, total_bars),
                     bars_per_second=COALESCE(?, bars_per_second),
                     eta_seconds=COALESCE(?, eta_seconds),
                     current_symbol=COALESCE(?, current_symbol),
                     current_timestamp=COALESCE(?, current_timestamp),
                     updated_at=?
                   WHERE job_id=?""",
                (
                    json.dumps(progress, default=str),
                    _as_int(processed), _as_int(total),
                    _as_float(bps), _as_float(eta),
                    progress.get("symbol"), progress.get("current_timestamp"),
                    self._now_iso(), job_id,
                ),
            )

    def set_result(self, job_id: str, result: Dict[str, Any], status: str = "COMPLETED") -> None:
        with self._transaction() as conn:
            conn.execute(
                """UPDATE backtest_jobs SET
                     result=?, status=?, phase=?, error=NULL, error_details=NULL,
                     completed_at=COALESCE(completed_at, ?), updated_at=?
                   WHERE job_id=?""",
                (json.dumps(result, default=str), normalize_status(status), status,
                 self._now_iso(), self._now_iso(), job_id),
            )

    def set_error(self, job_id: str, error: Any, error_details: Any = None) -> None:
        err_str = error.get("message") if isinstance(error, dict) else str(error)
        details = json.dumps(error_details if error_details is not None else error, default=str) \
            if (error_details is not None or isinstance(error, dict)) else None
        with self._transaction() as conn:
            conn.execute(
                """UPDATE backtest_jobs SET
                     status=?, phase='FAILED', error=?, error_details=?,
                     completed_at=COALESCE(completed_at, ?), updated_at=?
                   WHERE job_id=?""",
                ("FAILED", err_str, details, self._now_iso(), self._now_iso(), job_id),
            )

    def request_cancel(self, job_id: str) -> bool:
        with self._transaction() as conn:
            cur = conn.execute(
                "UPDATE backtest_jobs SET cancel_requested=1, updated_at=? WHERE job_id=?",
                (self._now_iso(), job_id),
            )
        return cur.rowcount > 0

    def recover_interrupted(self) -> List[str]:
        """Mark every job left in an active state as INTERRUPTED_BY_RESTART.

        Called once at backend startup. An interrupted job keeps its progress,
        error note, and cancel flag — but is never reported COMPLETED. Returns
        the recovered job ids (logged, and visible to the API/UI).
        """
        with self._transaction() as conn:
            rows = conn.execute(
                """SELECT job_id FROM backtest_jobs
                   WHERE status IN ('QUEUED','FETCHING_DATA','RESOLVING_CONTRACTS','RUNNING','FINALIZING')"""
            ).fetchall()
            ids = [r["job_id"] for r in rows]
            if ids:
                conn.execute(
                    """UPDATE backtest_jobs SET
                         status=?, phase='INTERRUPTED_BY_RESTART',
                         error=COALESCE(error, 'Backend restarted while this job was running — the job was interrupted and must be re-run.'),
                         completed_at=COALESCE(completed_at, ?),
                         updated_at=?
                       WHERE job_id IN ({})""".format(",".join("?" for _ in ids)),
                    [INTERRUPTED_BY_RESTART, self._now_iso(), self._now_iso(), *ids],
                )
        if ids:
            logger.warning(
                "BACKTEST_RESTART_RECOVERY recovered=%d job(s): %s", len(ids), ids
            )
        return ids


    def clear_all(self) -> None:
        """Delete every job row (test isolation only — never called in production)."""
        with self._transaction() as conn:
            conn.execute("DELETE FROM backtest_jobs")


class DuplicateActiveJobError(RuntimeError):
    """A job is already active — DB-enforced across restarts."""


def _as_int(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _as_float(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _default_db_path() -> str:
    """Jobs DB lives next to the main DATABASE_PATH (same persistent disk in
    production; per-process tmp dir under the test suite, which sets
    DATABASE_PATH — so tests are isolated with zero configuration)."""
    base = os.getenv("DATABASE_PATH", "data/trading_bot.db")
    directory = os.path.dirname(base) or "data"
    return os.path.join(directory, "backtest_jobs.db")


# Module-level singleton (same pattern as the rest of this codebase).
job_store = BacktestJobStore(_default_db_path())
