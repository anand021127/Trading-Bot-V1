"""Tests for the consolidated single-process architecture.

Root cause this fixes: the trading loop used to run in a separate
backend/worker.py OS process, meaning BotState lived independently in
each process's memory — the dashboard's Start/Stop/Kill had no effect on
whether the actual trading loop (in the other process) did anything.
The trading loop now runs as an in-process background asyncio task
inside the same web service, the same way the live scanner already does.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from backend.api.main import app


def test_trading_loop_runs_as_in_process_background_task() -> None:
    """This is the actual regression test for the architecture fix: the
    trading session must be a background task on THIS app's event loop,
    not something requiring a separate process to be running."""
    with TestClient(app) as client:
        # App booted successfully with the engine + trading task wired in.
        response = client.get("/api/version")
        assert response.status_code == 200
        assert response.json()["engine_active"] is True

        # The task itself should exist and not be immediately finished/
        # crashed (run_trading_session() is an infinite loop while
        # BotState.is_running() — or idles waiting for it — so it should
        # still be pending right after startup).
        task = app.state.trading_task
        assert task is not None
        assert not task.done()


def test_shutdown_cleanly_cancels_the_trading_task() -> None:
    """The lifespan's shutdown path must cancel the background task
    rather than leaving it orphaned when the app stops."""
    with TestClient(app):
        pass  # __exit__ triggers lifespan shutdown

    # After the context manager exits, the task should have been
    # cancelled (not left running forever in the background).
    task = app.state.trading_task
    assert task is not None
    assert task.cancelled() or task.done()
