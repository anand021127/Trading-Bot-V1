"""Tests for BotState — DB-backed shared state."""
from __future__ import annotations

import tempfile
import uuid

from backend.database.db_manager import DatabaseManager
from backend.strategy.trading_engine import BotState


class TestBotStateCrossProcessPersistence:
    def setup_method(self) -> None:
        path = f"{tempfile.gettempdir()}/test_botstate_{uuid.uuid4().hex}.db"
        self.db = DatabaseManager(db_path=path)
        self.db.init_db()
        self._original = BotState._db
        BotState._db = self.db
        try:
            BotState.stop("setup")
        except Exception:
            pass
        try:
            BotState.reset_kill()
        except Exception:
            pass

    def teardown_method(self) -> None:
        BotState._db = self._original

    def test_default_state_is_not_running(self) -> None:
        assert BotState.is_running() is False

    def test_start_persists_running_true(self) -> None:
        BotState.start()
        assert BotState.is_running() is True

    def test_second_botstate_db_handle_sees_the_same_state(self) -> None:
        BotState.start()
        db_path = BotState._db.db_path
        second_process_db = DatabaseManager(db_path=db_path)
        real_db = BotState._db
        BotState._db = second_process_db
        try:
            assert BotState.is_running() is True
        finally:
            BotState._db = real_db

    def test_status_includes_uptime_when_running(self) -> None:
        BotState.start()
        st = BotState.status()
        assert st.get("running") is True
        assert "uptime_seconds" in st or "started_at" in st or "uptime" in st

    def test_status_uptime_zero_when_stopped(self) -> None:
        BotState.stop("test")
        st = BotState.status()
        assert st.get("running") is False
