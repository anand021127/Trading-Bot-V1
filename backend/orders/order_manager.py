"""Order manager — paper and live adapters share this API."""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from backend.orders.order_models import (
    Order,
    OrderRequest,
    OrderStatus,
    normalize_broker_status,
)

logger = logging.getLogger(__name__)


class OrderError(Exception):
    pass


class OrderManager:
    def __init__(
        self,
        client: Any = None,
        paper_mode: bool = True,
        paper_slippage_pct: float = 0.1,
        default_product: Optional[str] = None,
    ) -> None:
        self.client = client
        self.paper_mode = paper_mode
        self.paper_slippage_pct = paper_slippage_pct
        self.default_product = default_product

    def _slippage_fraction(self) -> float:
        pct = float(self.paper_slippage_pct or 0.0)
        # 0.1 means 0.1% if <= 5, else treat as already-fraction / percent points
        if pct > 5:
            return pct / 100.0
        return pct / 100.0

    def place_order(self, request: OrderRequest) -> Order:
        if request.quantity <= 0:
            raise OrderError("quantity must be positive")
        side = (request.side or "").upper()
        if side not in ("BUY", "SELL"):
            raise OrderError(f"invalid side {request.side}")

        product = request.product or self.default_product
        if not self.paper_mode and not product:
            raise OrderError("Live orders require an explicit product (I or D). Refusing library default.")

        if self.paper_mode:
            return self._paper_fill(request)

        if self.client is None:
            raise OrderError("Live OrderManager requires a broker client")

        tag = request.tag or request.signal_id or f"sig-{uuid.uuid4().hex[:16]}"
        try:
            raw = self.client.place_order(
                symbol=request.symbol,
                transaction_type=side,
                quantity=int(request.quantity),
                order_type=request.order_type or "MARKET",
                price=float(request.price or 0.0),
                product=product,
                instrument_key=request.instrument_key,
            )
        except Exception as exc:
            logger.error("place_order failed (token hidden): %s", type(exc).__name__)
            raise OrderError(f"broker place_order failed: {type(exc).__name__}") from exc

        order_id = None
        if isinstance(raw, dict):
            order_id = raw.get("order_id") or (raw.get("data") or {}).get("order_id")
        if not order_id:
            return Order(
                id="",
                symbol=request.symbol,
                status=OrderStatus.UNKNOWN,
                quantity=request.quantity,
                remaining_quantity=request.quantity,
                raw=raw if isinstance(raw, dict) else {"raw": str(raw)},
            )

        details: Dict[str, Any] = {}
        try:
            if hasattr(self.client, "get_order_details"):
                details = self.client.get_order_details(order_id) or {}
        except Exception as exc:
            logger.error("get_order_details failed for %s: %s", order_id, type(exc).__name__)
            return Order(
                id=str(order_id),
                symbol=request.symbol,
                status=OrderStatus.SUBMITTED,
                quantity=request.quantity,
                remaining_quantity=request.quantity,
                raw={"place": raw, "details_error": type(exc).__name__},
            )

        status = normalize_broker_status(details.get("status") or raw.get("status"))
        filled = int(details.get("filled_quantity") or 0)
        qty = int(details.get("quantity") or request.quantity)
        avg = float(details.get("average_price") or request.price or 0.0)
        remaining = max(0, qty - filled)
        if status == OrderStatus.UNKNOWN and filled >= qty > 0:
            status = OrderStatus.FILLED
        return Order(
            id=str(order_id),
            symbol=request.symbol,
            status=status,
            filled_quantity=filled,
            remaining_quantity=remaining,
            quantity=qty,
            price=avg,
            average_price=avg,
            raw={"place": raw, "details": details, "tag": tag, "product": product},
        )

    def _paper_fill(self, request: OrderRequest) -> Order:
        side = request.side.upper()
        ltp = float(request.price or 0.0)
        bid = ask = None
        if self.client is not None and request.instrument_key and hasattr(self.client, "get_quote_by_instrument_key"):
            try:
                q = self.client.get_quote_by_instrument_key(request.instrument_key) or {}
                ltp = float(q.get("ltp") or ltp or 0.0)
                bid = q.get("bid_price")
                ask = q.get("ask_price")
            except Exception:
                pass
        slip = self._slippage_fraction()
        if side == "BUY":
            base = float(ask if ask is not None else ltp)
            fill = base * (1.0 + slip)
        else:
            base = float(bid if bid is not None else ltp)
            fill = base * (1.0 - slip)
        return Order(
            id=f"PAPER-{uuid.uuid4().hex[:12]}",
            symbol=request.symbol,
            status=OrderStatus.FILLED,
            filled_quantity=int(request.quantity),
            remaining_quantity=0,
            quantity=int(request.quantity),
            price=fill,
            average_price=fill,
            fill_details={"fill_model": "paper_realistic", "base": base, "slippage_pct": slip * 100},
        )
