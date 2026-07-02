"""Tests for trading router endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from backend.api.main import app
import backend.api.routers.trading as trading_router


def test_trading_router_get_positions_returns_200(monkeypatch) -> None:
    position = MagicMock()
    position.symbol = "NSE:INFY"
    position.quantity = 1
    position.average_price = 100.0
    position.entry_time = "2026-07-02T00:00:00Z"
    position.side = "long"
    position.unrealized_pnl = 0.0

    monkeypatch.setattr(trading_router.engine, "list_positions", lambda: [position])

    client = TestClient(app)
    response = client.get("/api/trading/positions")

    assert response.status_code == 200
    assert response.json() == [
        {
            "symbol": "NSE:INFY",
            "quantity": 1,
            "average_price": 100.0,
            "entry_time": "2026-07-02T00:00:00Z",
            "side": "long",
            "unrealized_pnl": 0.0,
        }
    ]


def test_trading_router_execute_trade_returns_200(monkeypatch) -> None:
    order = MagicMock()
    order.id = "order-456"
    order.symbol = "NSE:INFY"
    order.side = "buy"
    order.quantity = 1
    order.price = 100.0
    order.order_type = "market"
    order.status = "filled"

    monkeypatch.setattr(trading_router.engine, "place_order", lambda symbol, side, quantity, price, order_type: order)

    client = TestClient(app)
    response = client.post(
        "/api/trading/execute",
        json={"symbol": "NSE:INFY", "side": "buy", "quantity": 1, "price": 100.0},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "executed"
    assert response.json()["order"]["id"] == "order-456"
