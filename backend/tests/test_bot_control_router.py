"""Tests for the bot control router (start/stop/kill/status).

These exercise the actual HTTP endpoints against the real (now DB-backed)
BotState — including a regression test for a bug caught while fixing
BotState: bot_control.py directly accessed the old in-process
`BotState._kill_switch` attribute, which no longer exists after BotState
became DB-backed, and would have raised AttributeError on every
POST /api/bot/start call.
"""
from __future__ import annotations

import tempfile
import uuid

import pytest
from fastapi.testclient import TestClient

from backend.api.main import app
from backend.database.db_manager import DatabaseManager
from backend.strategy.trading_engine import BotState


@pytest.fixture(autouse=True)
def isolated_botstate_db():
    path = f"{tempfile.gettempdir()}/test_bot_control_{uuid.uuid4().hex}.db"
    db = DatabaseManager(db_path=path)
    db.init_db()
    original = BotState._db
    BotState._db = db
    yield
    BotState._db = original


class TestBotControlRouter:
    def test_status_endpoint_works_with_no_engine_and_no_prior_state(self) -> None:
        client = TestClient(app)
        response = client.get("/api/bot/status")
        assert response.status_code == 200
        data = response.json()
        assert data["running"] is False
        assert "mode" in data

    def test_start_then_status_shows_running(self) -> None:
        client = TestClient(app)
        start_response = client.post("/api/bot/start")
        assert start_response.status_code == 200
        assert start_response.json()["success"] is True

        status_response = client.get("/api/bot/status")
        assert status_response.json()["running"] is True

    def test_starting_twice_reports_already_running(self) -> None:
        client = TestClient(app)
        client.post("/api/bot/start")
        second = client.post("/api/bot/start")
        assert second.json()["success"] is False
        assert "already running" in second.json()["message"].lower()

    def test_stop_when_not_running_reports_not_running(self) -> None:
        client = TestClient(app)
        response = client.post("/api/bot/stop")
        assert response.json()["success"] is False

    def test_start_then_stop_then_status_shows_not_running(self) -> None:
        client = TestClient(app)
        client.post("/api/bot/start")
        stop_response = client.post("/api/bot/stop")
        assert stop_response.json()["success"] is True

        status_response = client.get("/api/bot/status")
        assert status_response.json()["running"] is False

    def test_kill_switch_blocks_starting_again_without_reset(self) -> None:
        """Regression test: this endpoint used to directly read the removed
        BotState._kill_switch class attribute and would raise AttributeError
        here instead of correctly blocking the start."""
        client = TestClient(app)
        client.post("/api/bot/start")
        kill_response = client.post("/api/bot/kill")
        assert kill_response.json()["success"] is True

        start_response = client.post("/api/bot/start")
        assert start_response.status_code == 200  # must not 500
        assert start_response.json()["success"] is False
        assert "kill switch" in start_response.json()["message"].lower()

    def test_reset_kill_allows_starting_again(self) -> None:
        client = TestClient(app)
        client.post("/api/bot/start")
        client.post("/api/bot/kill")
        reset_response = client.post("/api/bot/reset-kill")
        assert reset_response.json()["success"] is True

        start_response = client.post("/api/bot/start")
        assert start_response.json()["success"] is True

    def test_status_reflects_state_set_via_a_second_botstate_handle(self) -> None:
        """Simulates the worker process (a separate OS process with its own
        DatabaseManager instance) writing state that the web process's
        dashboard status endpoint must then see."""
        client = TestClient(app)
        db_path = BotState._db.db_path
        worker_side_db = DatabaseManager(db_path=db_path)

        real_db = BotState._db
        BotState._db = worker_side_db
        try:
            BotState.start()
        finally:
            BotState._db = real_db

        status_response = client.get("/api/bot/status")
        assert status_response.json()["running"] is True
