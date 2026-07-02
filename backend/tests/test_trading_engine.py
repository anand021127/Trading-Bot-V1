"""Tests for the trading engine integration layer."""

from __future__ import annotations

from unittest.mock import MagicMock

from backend.strategy.trading_engine import TradingEngine
from backend.orders.order_models import Order


def test_trading_engine_places_order_and_persists_trade() -> None:
    order_manager = MagicMock()
    order_manager.place_order.return_value = Order(
        id="order-123",
        symbol="NSE:INFY",
        side="buy",
        quantity=1,
        price=100.0,
        order_type="market",
        status="filled",
    )
    db_manager = MagicMock()

    engine = TradingEngine(order_manager=order_manager, db_manager=db_manager)
    order = engine.place_order(symbol="NSE:INFY", side="buy", quantity=1, price=100.0)

    order_manager.place_order.assert_called_once()
    db_manager.insert_trade.assert_called_once()
    assert order.id == "order-123"
    assert order.symbol == "NSE:INFY"


def test_trading_engine_list_positions_returns_list() -> None:
    db_manager = MagicMock()
    db_manager.list_positions.return_value = []
    engine = TradingEngine(db_manager=db_manager)

    assert engine.list_positions() == []
