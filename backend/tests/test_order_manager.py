"""Tests for the order manager."""

from __future__ import annotations

from unittest.mock import MagicMock

from backend.orders.order_manager import OrderManager
from backend.orders.order_models import OrderRequest


def test_place_order_builds_order_from_response() -> None:
    client = MagicMock()
    client.get.return_value = {"order_id": "123", "status": "filled"}
    manager = OrderManager(client=client)

    order = manager.place_order(OrderRequest(symbol="NSE:INFY", side="buy", quantity=1))

    assert order.id == "123"
    assert order.status == "filled"
    assert order.symbol == "NSE:INFY"
