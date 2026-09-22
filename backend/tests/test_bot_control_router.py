"""Tests for the bot control router (start/stop/kill/status).

Paper mode intentionally requires PaperTradingRuntime to be attached.
These tests attach a lightweight runtime stub so START is allowed without
weakening production safety (no runtime → start refused is also covered).
"""
from __future__ import annotations

import tempfile
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.api.main import app
from backend.database.db_manager import DatabaseManager
from backend.strategy.trading_engine import BotState


class _PaperRuntimeStub:
    def kill(self):
        return None


class TestBotControlRouter:
    def setup_method(self) -> None:
        path = f"{tempfile.gettempdir()}/test_bot_control_{uuid.uuid4().hex}.db"
        self.db = DatabaseManager(db_path=path)
        self.db.init_db()
        self._original_db = BotState._db
        BotState._db = self.db
        try:
            BotState.reset_kill()
        except Exception:
            pass
        try:
            BotState.stop("test setup")
        except Exception:
            pass
        # Attach paper runtime for paper-mode START
        self._rt = _PaperRuntimeStub()
        app.state.paper_runtime = self._rt
        self._patcher = patch("backend.api.routers.bot_control._paper_runtime_from_app", return_value=self._rt)
        self._patcher.start()
        # Ensure settings.mode is paper
        self._mode_patch = patch("backend.api.routers.bot_control.settings")
        mock_settings = self._mode_patch.start()
        mock_settings.mode = "paper"

    def teardown_method(self) -> None:
        self._patcher.stop()
        self._mode_patch.stop()
        BotState._db = self._original_db
        if hasattr(app.state, "paper_runtime"):
            app.state.paper_runtime = None

    def test_status_endpoint_works_with_no_engine_and_no_prior_state(self) -> None:
        client = TestClient(app)
        response = client.get("/api/bot/status")
        assert response.status_code == 200
        data = response.json()
        assert data["running"] is False
        assert "mode" in data

    def test_start_without_runtime_is_refused(self) -> None:
        self._patcher.stop()
        with patch("backend.api.routers.bot_control._paper_runtime_from_app", return_value=None):
            client = TestClient(app)
            response = client.post("/api/bot/start")
        self._patcher.start()
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is False
        assert "papertradingruntime" in body["message"].lower() or "not attached" in body["message"].lower()

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
        client = TestClient(app)
        client.post("/api/bot/start")
        kill_response = client.post("/api/bot/kill")
        assert kill_response.json()["success"] is True

        start_response = client.post("/api/bot/start")
        assert start_response.status_code == 200
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
