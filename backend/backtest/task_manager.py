"""Background task manager for backtests.

Root cause of the "timeout of 30000ms exceeded" bug: the frontend's axios
client has a 30s request timeout, and POST /api/backtest/run used to fetch
a full year of real Upstox candle data (chunked into ~15 sequential API
calls for 5-minute granularity) AND run the full multi-strategy engine
over every bar synchronously, all inside that one request — routinely
taking well over 30 seconds for a full year of intraday data.

Fix: /run now only *starts* the backtest and returns a task_id
immediately. The actual fetch + simulation happens in a background
asyncio task, with progress polled via /status/{task_id} and the final
result fetched via /result/{task_id} once done. No in-process work ever
blocks a single HTTP request for more than a moment.

This is intentionally an in-memory task store (matching this project's
existing single-process Render deployment — no Redis/Celery broker is
provisioned), which is consistent with the rest of the app's tenant model.
Tasks are not persisted across a server restart; a restart mid-backtest
means re-running it, which is a reasonable tradeoff for a single free-tier
web service and is clearly reported via task status rather than silently
losing progress.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STATUS_QUEUED = "queued"
STATUS_FETCHING_DATA = "fetching_data"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

# Tasks older than this are evicted on the next cleanup pass so the
# in-memory store doesn't grow unbounded across a long-lived process.
TASK_RETENTION_SECONDS = 2 * 60 * 60  # 2 hours


@dataclass
class BacktestTask:
    task_id: str
    status: str = STATUS_QUEUED
    progress: Dict[str, Any] = field(default_factory=dict)
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)
    updated_at: float = field(default_factory=time.monotonic)

    def to_status_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "progress": self.progress,
            "error": self.error,
            "elapsed_seconds": round(time.monotonic() - self.created_at, 1),
        }


class BacktestTaskManager:
    def __init__(self) -> None:
        self._tasks: Dict[str, BacktestTask] = {}

    def _evict_old_tasks(self) -> None:
        cutoff = time.monotonic() - TASK_RETENTION_SECONDS
        stale = [tid for tid, t in self._tasks.items() if t.updated_at < cutoff]
        for tid in stale:
            del self._tasks[tid]

    def create_task(self) -> BacktestTask:
        self._evict_old_tasks()
        task = BacktestTask(task_id=str(uuid.uuid4()))
        self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> Optional[BacktestTask]:
        return self._tasks.get(task_id)

    def update_progress(self, task_id: str, progress: Dict[str, Any], status: Optional[str] = None) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.progress = progress
        if status:
            task.status = status
        task.updated_at = time.monotonic()

    def complete(self, task_id: str, result: Dict[str, Any]) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.status = STATUS_COMPLETED
        task.result = result
        task.updated_at = time.monotonic()

    def fail(self, task_id: str, error: str) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.status = STATUS_FAILED
        task.error = error
        task.updated_at = time.monotonic()


# Module-level singleton — same pattern as the rest of this codebase.
task_manager = BacktestTaskManager()


async def run_backtest_in_background(
    task_id: str,
    client: Any,
    engine: Any,
    symbols: List[str],
    interval: str,
    start_date: str,
    end_date: str,
    strategy_names: Optional[List[str]],
) -> None:
    """The actual long-running work, run as an asyncio background task
    (not blocking any HTTP request). Fetches real candles per symbol
    (never fabricated — a fetch failure for a symbol just means it's
    skipped/reported, not padded with synthetic data), then runs the
    engine with a progress callback wired to the task manager."""
    try:
        task_manager.update_progress(
            task_id, {"phase": "fetching_data", "symbols_fetched": 0, "total_symbols": len(symbols)},
            status=STATUS_FETCHING_DATA,
        )

        symbol_candles: Dict[str, List[Dict[str, Any]]] = {}
        fetch_errors: List[Dict[str, str]] = []

        for i, sym in enumerate(symbols):
            try:
                candles = await asyncio.to_thread(
                    client.get_historical_candles_full_range, sym, interval, start_date, end_date,
                )
                symbol_candles[sym] = candles
            except Exception as e:
                fetch_errors.append({"symbol": sym, "error": str(e)})
                logger.warning("Backtest candle fetch failed for %s: %s", sym, e)

            task_manager.update_progress(task_id, {
                "phase": "fetching_data", "symbols_fetched": i + 1, "total_symbols": len(symbols),
            })

        if not symbol_candles or all(len(c) == 0 for c in symbol_candles.values()):
            task_manager.fail(
                task_id,
                f"Could not fetch any real historical candles for {symbols}. "
                f"Errors: {fetch_errors}. Refusing to fabricate results.",
            )
            return

        task_manager.update_progress(
            task_id, {"phase": "processing", "total_symbols": len(symbol_candles)},
            status=STATUS_RUNNING,
        )

        def progress_callback(p: Dict[str, Any]) -> None:
            task_manager.update_progress(task_id, {"phase": "processing", **p})

        backtest_result = await asyncio.to_thread(
            engine.run, symbol_candles, strategy_names, progress_callback,
        )

        payload = backtest_result.to_dict()
        payload["fetch_errors"] = fetch_errors
        payload["symbols_requested"] = symbols
        payload["date_range"] = {"start": start_date, "end": end_date}
        payload["interval"] = interval
        if backtest_result.trades_taken == 0 and not backtest_result.skipped_symbols:
            payload["message"] = (
                "Real candle data was processed but no strategy conditions were met "
                "in this window — see rejection_reason_counts for exactly why. This "
                "is a genuine result, not an error."
            )

        task_manager.complete(task_id, payload)

    except Exception as e:
        logger.exception("Background backtest task %s failed", task_id)
        task_manager.fail(task_id, str(e))
