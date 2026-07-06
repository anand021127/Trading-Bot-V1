"""Unit tests for the database manager."""

from __future__ import annotations

from backend.database.db_manager import DatabaseManager
from backend.database.models import Position, Trade


def test_database_manager_creates_tables_and_persists_trade() -> None:
    """The database manager should create tables and persist trades."""
    manager = DatabaseManager(db_path=":memory:")

    manager.init_db()
    manager.insert_trade(
        Trade(
            id="trade-1",
            symbol="NIFTY",
            side="buy",
            quantity=50,
            price=22000.0,
            timestamp=manager._now(),
        )
    )

    trades = manager.list_trades()
    assert len(trades) == 1
    assert trades[0].symbol == "NIFTY"


def test_database_manager_upserts_position() -> None:
    """The database manager should insert or update positions."""
    manager = DatabaseManager(db_path=":memory:")

    manager.init_db()
    manager.upsert_position(
        Position(
            symbol="BANKNIFTY",
            quantity=10,
            average_price=48000.0,
            entry_time=manager._now(),
        )
    )

    positions = manager.list_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "BANKNIFTY"
