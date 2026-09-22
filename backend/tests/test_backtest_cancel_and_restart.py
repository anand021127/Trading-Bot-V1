"""Backtest job cancel + in-memory restart semantics."""
from __future__ import annotations

from backend.backtest.task_manager import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_RUNNING,
    BacktestTaskManager,
    task_manager,
)


def test_cancel_running_job_stops_status():
    task = task_manager.create_task(
        symbols=["NIFTY50"],
        start_date="2024-10-01",
        end_date="2024-10-02",
        interval="5minute",
        prevent_duplicates=False,
    )
    task_manager.update_progress(task.task_id, {"phase": "processing"}, status=STATUS_RUNNING)
    returned = task_manager.cancel(task.task_id)
    assert returned is not None
    rec = task_manager.get(task.task_id)
    assert rec.status == STATUS_CANCELLED
    assert rec._cancelled is True


def test_cannot_cancel_completed_job_back_to_cancelled():
    task = task_manager.create_task(
        symbols=["BANKNIFTY"],
        start_date="2024-10-01",
        end_date="2024-10-02",
        interval="5minute",
        prevent_duplicates=False,
    )
    rec = task_manager.get(task.task_id)
    rec.status = STATUS_COMPLETED
    rec.result = {"ok": True}
    ok = rec.cancel("late")
    assert ok is False
    assert task_manager.get(task.task_id).status == STATUS_COMPLETED


def test_restart_drops_in_memory_running_jobs():
    """Jobs live in-process only. A new BacktestTaskManager has no RUNNING orphans."""
    fresh = BacktestTaskManager()
    assert fresh.get_active_task() is None
