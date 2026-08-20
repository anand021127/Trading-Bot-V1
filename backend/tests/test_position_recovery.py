"""Tests for worker position recovery and broker reconciliation."""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import backend.strategy.trading_engine as te
from backend.database.db_manager import DatabaseManager
from backend.database.models import Position
from backend.strategy.trading_engine import TradingEngine


@pytest.fixture(autouse=True)
def set_live_mode():
    orig_mode = te.settings.mode
    te.settings.mode = "live"
    yield
    te.settings.mode = orig_mode


@pytest.fixture
def memory_db():
    db = DatabaseManager(":memory:")
    db.init_db()
    return db


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.get_positions_with_details.return_value = []
    client.get_profile.return_value = {"user_name": "Test User"}
    client.get_market_status.return_value = {"status": "open"}
    return client


def test_recovery_no_open_positions(memory_db, mock_client):
    """Scenario 6: No open positions locally or at broker."""
    mock_client.get_positions_with_details.return_value = []

    engine = TradingEngine(client=mock_client, db_manager=memory_db)
    res = engine.hydrate_and_reconcile_positions()

    assert res["status"] == "reconciled"
    assert res["mismatch"] is False
    assert res["positions_count"] == 0
    assert len(engine._open_positions) == 0
    assert engine._position_mismatch is False


def test_recovery_matching_single_position(memory_db, mock_client):
    """Scenario 1: Restart with matching local and broker position."""
    pos = Position(
        symbol="NSE_FO|54321",
        quantity=50,
        average_price=120.0,
        entry_time=datetime.now(timezone.utc),
        side="long",
        unrealized_pnl=0.0,
    )
    memory_db.upsert_position(pos)

    mock_client.get_positions_with_details.return_value = [
        {
            "instrument_key": "NSE_FO|54321",
            "trading_symbol": "NIFTY24AUG22000CE",
            "quantity": 50,
            "average_price": 120.0,
        }
    ]

    engine = TradingEngine(client=mock_client, db_manager=memory_db)
    res = engine.hydrate_and_reconcile_positions()

    assert res["status"] == "reconciled"
    assert res["mismatch"] is False
    assert res["positions_count"] == 1
    assert "NSE_FO|54321" in engine._open_positions
    recovered = engine._open_positions["NSE_FO|54321"]
    assert recovered["quantity"] == 50
    assert recovered["entry_price"] == 120.0
    assert recovered["stop_loss"] > 0
    assert recovered["target"] > recovered["entry_price"]
    assert engine._position_mismatch is False


def test_recovery_broker_position_missing_locally(memory_db, mock_client):
    """Scenario 2: Restart with broker position missing locally in SQLite."""
    # Local DB is empty
    assert len(memory_db.get_open_positions()) == 0

    mock_client.get_positions_with_details.return_value = [
        {
            "instrument_key": "NSE_FO|99999",
            "trading_symbol": "BANKNIFTY24AUG50000CE",
            "quantity": 25,
            "average_price": 350.0,
        }
    ]

    engine = TradingEngine(client=mock_client, db_manager=memory_db)
    res = engine.hydrate_and_reconcile_positions()

    assert res["status"] == "mismatch"
    assert res["mismatch"] is True
    assert "missing locally" in res["reason"]
    assert engine._position_mismatch is True
    # Verify new orders are blocked
    fake_signal = MagicMock()
    fake_signal.signal = "BUY"
    fake_signal.symbol = "NSE_INDEX|Nifty 50"
    assert engine.execute_multi_signal(fake_signal) is None


def test_recovery_local_position_missing_at_broker(memory_db, mock_client):
    """Scenario 3: Restart with local position in SQLite missing at broker."""
    pos = Position(
        symbol="NSE_FO|11111",
        quantity=50,
        average_price=85.0,
        entry_time=datetime.now(timezone.utc),
        side="long",
        unrealized_pnl=0.0,
    )
    memory_db.upsert_position(pos)

    # Broker has 0 open positions
    mock_client.get_positions_with_details.return_value = []

    engine = TradingEngine(client=mock_client, db_manager=memory_db)
    res = engine.hydrate_and_reconcile_positions()

    assert res["status"] == "mismatch"
    assert res["mismatch"] is True
    assert "missing at broker" in res["reason"]
    assert engine._position_mismatch is True


def test_recovery_quantity_mismatch(memory_db, mock_client):
    """Scenario 4: Quantity mismatch between local and broker."""
    pos = Position(
        symbol="NSE_FO|54321",
        quantity=50,
        average_price=120.0,
        entry_time=datetime.now(timezone.utc),
        side="long",
        unrealized_pnl=0.0,
    )
    memory_db.upsert_position(pos)

    # Broker has different quantity (e.g. 100)
    mock_client.get_positions_with_details.return_value = [
        {
            "instrument_key": "NSE_FO|54321",
            "trading_symbol": "NIFTY24AUG22000CE",
            "quantity": 100,
            "average_price": 120.0,
        }
    ]

    engine = TradingEngine(client=mock_client, db_manager=memory_db)
    res = engine.hydrate_and_reconcile_positions()

    assert res["status"] == "mismatch"
    assert res["mismatch"] is True
    assert "Quantity mismatch" in res["reason"]
    assert engine._position_mismatch is True


def test_recovery_multiple_open_positions(memory_db, mock_client):
    """Scenario 5: Multiple matching open positions."""
    pos1 = Position(
        symbol="NSE_FO|11111",
        quantity=50,
        average_price=100.0,
        entry_time=datetime.now(timezone.utc),
        side="long",
        unrealized_pnl=0.0,
    )
    pos2 = Position(
        symbol="NSE_FO|22222",
        quantity=25,
        average_price=200.0,
        entry_time=datetime.now(timezone.utc),
        side="long",
        unrealized_pnl=0.0,
    )
    memory_db.upsert_position(pos1)
    memory_db.upsert_position(pos2)

    mock_client.get_positions_with_details.return_value = [
        {
            "instrument_key": "NSE_FO|11111",
            "trading_symbol": "NIFTY24AUG22000CE",
            "quantity": 50,
            "average_price": 100.0,
        },
        {
            "instrument_key": "NSE_FO|22222",
            "trading_symbol": "BANKNIFTY24AUG50000PE",
            "quantity": 25,
            "average_price": 200.0,
        },
    ]

    engine = TradingEngine(client=mock_client, db_manager=memory_db)
    res = engine.hydrate_and_reconcile_positions()

    assert res["status"] == "reconciled"
    assert res["mismatch"] is False
    assert res["positions_count"] == 2
    assert len(engine._open_positions) == 2
    assert "NSE_FO|11111" in engine._open_positions
    assert "NSE_FO|22222" in engine._open_positions
    assert engine._position_mismatch is False

