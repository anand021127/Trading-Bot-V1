"""Order execution helpers for the trading bot."""

from __future__ import annotations

from typing import Dict, Optional

from backend.orders.order_models import Order, OrderRequest
from backend.broker.upstox_client import UpstoxClient


class OrderManager:
    """Manage order submissions and confirmations."""

    def __init__(self, client: Optional[UpstoxClient] = None):
        self.client = client or UpstoxClient()

    def place_order(self, request: OrderRequest) -> Order:
        payload = {
            "symbol": request.symbol,
            "side": request.side,
            "quantity": request.quantity,
            "order_type": request.order_type,
        }
        if request.price is not None:
            payload["price"] = request.price

        response = self.client.get("/orders", params=payload)
        return Order(
            id=str(response.get("order_id", "")),
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            price=request.price,
            order_type=request.order_type,
            status=response.get("status", "unknown"),
        )
