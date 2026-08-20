"""Tests for the order manager."""

from __future__ import annotations

from unittest.mock import MagicMock

from backend.orders.order_manager import OrderManager
from backend.orders.order_models import OrderRequest, OrderStatus


def test_place_order_builds_order_from_response() -> None:
    client = MagicMock()
    client.place_order.return_value = {"success": True, "order_id": "123", "status": "filled"}
    client.get_order_details.return_value = {
        "order_id": "123", "status": "COMPLETE",
        "average_price": 100.0, "filled_quantity": 1, "quantity": 1,
    }
    manager = OrderManager(client=client, paper_mode=False)

    order = manager.place_order(OrderRequest(symbol="NSE_FO|OPTION", side="buy", quantity=1))

    assert order.id == "123"
    assert order.status == OrderStatus.FILLED
    assert order.symbol == "NSE_FO|OPTION"
    assert order.filled_quantity == 1

