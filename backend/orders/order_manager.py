"""Production order manager — validates, executes, confirms, and tracks orders."""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional, Set

from backend.broker.upstox_client import UpstoxClient, UpstoxAPIError
from backend.orders.order_models import Order, OrderRequest

logger = logging.getLogger(__name__)

# In-memory duplicate prevention (per session)
_pending_orders: Set[str] = set()


class OrderError(Exception):
    """Raised when an order cannot be placed or confirmed."""


class OrderManager:
    """
    Professional order execution pipeline:

    Signal → Risk check → Place order → Verify fill → Track position → Log
    """

    def __init__(
        self,
        client: Optional[UpstoxClient] = None,
        paper_mode: bool = True,
        verify_timeout_seconds: int = 15,
    ) -> None:
        self.client = client or UpstoxClient()
        self.paper_mode = paper_mode
        self.verify_timeout = verify_timeout_seconds
        self._paper_orders: Dict[str, Order] = {}
        self._paper_order_counter = 0

    # ─── Place Order ──────────────────────────────────────────────────────────

    def place_order(self, request: OrderRequest) -> Order:
        """
        Place an order through the complete safety pipeline:
        1. Validate request
        2. Prevent duplicates
        3. Execute (paper or live)
        4. Verify fill
        5. Return confirmed Order
        """
        self._validate_request(request)
        dedup_key = f"{request.symbol}:{request.side}:{request.quantity}"

        if dedup_key in _pending_orders:
            raise OrderError(f"Duplicate order blocked for {request.symbol} {request.side}")

        _pending_orders.add(dedup_key)
        try:
            if self.paper_mode:
                order = self._place_paper_order(request)
            else:
                order = self._place_live_order(request)

            logger.info(
                "Order placed | id=%s symbol=%s side=%s qty=%d status=%s paper=%s",
                order.id, order.symbol, order.side, order.quantity, order.status, self.paper_mode,
            )
            return order
        finally:
            _pending_orders.discard(dedup_key)

    # ─── Paper execution ──────────────────────────────────────────────────────

    def _place_paper_order(self, request: OrderRequest) -> Order:
        """Simulate order fill at requested price (or live LTP if not specified)."""
        self._paper_order_counter += 1
        order_id = f"PAPER-{self._paper_order_counter:05d}-{uuid.uuid4().hex[:6].upper()}"

        fill_price = request.price
        if fill_price is None or fill_price == 0.0:
            try:
                q = self.client.get_live_quote(request.symbol)
                fill_price = q.get("ltp", 0.0)
            except Exception:
                fill_price = 0.0

        order = Order(
            id=order_id,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            price=fill_price,
            order_type=request.order_type,
            status="filled",
            timestamp=datetime.now(timezone.utc),
        )
        self._paper_orders[order_id] = order
        logger.info("Paper order filled | %s @ ₹%.2f", order_id, fill_price or 0)
        return order

    # ─── Live execution ───────────────────────────────────────────────────────

    def _place_live_order(self, request: OrderRequest) -> Order:
        """Place a real order via Upstox and verify the fill."""
        try:
            response = self.client.place_order(
                symbol=request.symbol,
                transaction_type=request.side.upper(),
                quantity=request.quantity,
                order_type=request.order_type.upper(),
                price=request.price or 0.0,
            )
        except UpstoxAPIError as e:
            logger.error("Order placement failed: %s", e)
            raise OrderError(f"Broker rejected order: {e}") from e

        if not response.get("success"):
            raise OrderError(f"Order not confirmed: {response}")

        order_id = response.get("order_id", str(uuid.uuid4()))

        # Verify fill with polling
        fill_price = self._verify_fill(order_id, request)

        order = Order(
            id=order_id,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            price=fill_price,
            order_type=request.order_type,
            status="filled" if fill_price else "pending",
            timestamp=datetime.now(timezone.utc),
        )
        return order

    def _verify_fill(self, order_id: str, request: OrderRequest) -> Optional[float]:
        """Poll Upstox for fill confirmation. Returns fill price or None."""
        deadline = time.monotonic() + self.verify_timeout
        while time.monotonic() < deadline:
            try:
                details = self.client.get_order_details(order_id)
                status = details.get("status", "").upper()
                if status in ("COMPLETE", "FILLED"):
                    return float(details.get("average_price", 0) or 0)
                if status in ("REJECTED", "CANCELLED"):
                    reason = details.get("reject_reason", "unknown")
                    raise OrderError(f"Order {order_id} rejected: {reason}")
                time.sleep(1)
            except OrderError:
                raise
            except Exception as e:
                logger.warning("Fill verification error: %s", e)
                time.sleep(2)
        logger.warning("Order %s not confirmed within %ds", order_id, self.verify_timeout)
        return None

    # ─── Cancel ───────────────────────────────────────────────────────────────

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order."""
        if self.paper_mode:
            if order_id in self._paper_orders:
                self._paper_orders[order_id].status = "cancelled"
                logger.info("Paper order cancelled: %s", order_id)
                return True
            return False
        return self.client.cancel_order(order_id)

    # ─── Validation ───────────────────────────────────────────────────────────

    @staticmethod
    def _validate_request(request: OrderRequest) -> None:
        if not request.symbol:
            raise OrderError("Symbol is required")
        if request.side.upper() not in ("BUY", "SELL", "LONG", "SHORT"):
            raise OrderError(f"Invalid side: {request.side}")
        if request.quantity <= 0:
            raise OrderError(f"Quantity must be positive, got {request.quantity}")
