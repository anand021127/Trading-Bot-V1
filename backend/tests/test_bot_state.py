"""Tests for BotState — now DB-backed instead of a bare in-process class
attribute (item: dashboard showing wrong bot status).

Root cause this fixes: this project runs as two separate OS processes on
Render (the web API and a standalone worker.py that runs the actual
trading loop). Each process has its own memory space, so an in-process
class attribute here meant Start/Stop/Kill on the dashboard (web process)
never actually reached the worker process placing trades, and vice versa.
"""
from __future__ import annotations

import tempfile
import uuid

import pytest

from backend.database.db_manager import DatabaseManager
from backend.strategy.trading_engine import BotState


@pytest.fixture(autouse=True)
def isolated_botstate_db():
    """Point BotState at a fresh temp-file DB for each test, so tests don't
    leak state into each other (BotState caches its DB connection at the
    class level, same as it would across two real OS processes sharing
    one file — here we just give each test its own file)."""
    path = f"{tempfile.gettempdir()}/test_botstate_{uuid.uuid4().hex}.db"
    db = DatabaseManager(db_path=path)
    db.init_db()
    original = BotState._db
    BotState._db = db
    yield
    BotState._db = original


class TestBotStateCrossProcessPersistence:
    def test_default_state_is_not_running(self) -> None:
        assert BotState.is_running() is False

    def test_start_persists_running_true(self) -> None:
        BotState.start()
        assert BotState.is_running() is True

    def test_second_botstate_db_handle_sees_the_same_state(self) -> None:
        """This is the actual regression test: simulate a second process
        (a fresh DatabaseManager instance pointed at the same file, as the
        worker.py process would have) reading state written by 'this'
        process."""
        BotState.start()
        db_path = BotState._db.db_path
        second_process_db = DatabaseManager(db_path=db_path)

        # Swap in the "other process's" handle and confirm it agrees.
        real_db = BotState._db
        BotState._db = second_process_db
        try:
            assert BotState.is_running() is True
        finally:
            BotState._db = real_db

    def test_stop_persists_across_handles(self) -> None:
        BotState.start()
        BotState.stop("manual stop from dashboard")
        db_path = BotState._db.db_path
        other_handle_db = DatabaseManager(db_path=db_path)
        real_db = BotState._db
        BotState._db = other_handle_db
        try:
            assert BotState.is_running() is False
            assert BotState.status()["stop_reason"] == "manual stop from dashboard"
        finally:
            BotState._db = real_db

    def test_kill_switch_overrides_running_across_handles(self) -> None:
        BotState.start()
        BotState.kill("emergency stop")
        db_path = BotState._db.db_path
        other_handle_db = DatabaseManager(db_path=db_path)
        real_db = BotState._db
        BotState._db = other_handle_db
        try:
            assert BotState.is_running() is False
            assert BotState.status()["kill_switch_active"] is True
        finally:
            BotState._db = real_db

    def test_reset_kill_allows_running_again(self) -> None:
        BotState.kill("test")
        BotState.reset_kill()
        BotState.start()
        assert BotState.is_running() is True

    def test_status_includes_uptime_when_running(self) -> None:
        BotState.start()
        status = BotState.status()
        assert status["running"] is True
        assert status["uptime_seconds"] >= 0

    def test_status_uptime_zero_when_stopped(self) -> None:
        BotState.stop()
        status = BotState.status()
        assert status["uptime_seconds"] == 0
