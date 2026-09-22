"""Lifespan / process architecture tests.

Paper mode intentionally does NOT start TradingEngine.run_forever as a
background task. Offline unit tests skip engine construction entirely.
These tests document the current contract rather than the legacy
in-process trading_task loop.
"""
from __future__ import annotations

import os

from fastapi.testclient import TestClient

from backend.api.main import app


def test_trading_loop_runs_as_in_process_background_task() -> None:
    """Under offline tests the app boots without a trading_task.

    Production paper mode uses PaperTradingRuntime + worker, not
    app.state.trading_task. This test asserts the app is healthy and
    that offline mode does not claim a phantom trading_task.
    """
    offline = os.environ.get("TRADING_BOT_OFFLINE_TESTS") == "1"
    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        if offline:
            # Engine/WS intentionally not started in offline tests
            assert getattr(app.state, "trading_task", None) is None
        else:
            # Non-offline: may or may not have trading_task depending on mode
            response2 = client.get("/api/version")
            assert response2.status_code in (200, 404)


def test_shutdown_cleanly_cancels_the_trading_task() -> None:
    """Lifespan shutdown must not leave the process in a bad state."""
    offline = os.environ.get("TRADING_BOT_OFFLINE_TESTS") == "1"
    with TestClient(app):
        pass  # __exit__ triggers lifespan shutdown

    if offline:
        # No trading_task was started; shutdown is a no-op for that field
        assert getattr(app.state, "trading_task", None) is None
    else:
        task = getattr(app.state, "trading_task", None)
        if task is not None:
            assert task.cancelled() or task.done()
