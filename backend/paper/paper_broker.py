"""In-process paper broker. Confirmed fills only; never invents quantity.

Positions carry full option lifecycle state so the paper runtime can evaluate
SL / target / trailing stops against real subsequent mark prices (never using
entry price as a fake exit unless that is genuinely the latest mark).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PaperOrder:
    order_id: str
    instrument_key: str
    side: str
    requested_qty: int
    filled_qty: int
    avg_price: float
    status: str
    reason: str = ""


class PaperBroker:
    def __init__(self) -> None:
        self.orders: Dict[str, PaperOrder] = {}
        # instrument_key → full position state
        self.positions: Dict[str, Dict[str, Any]] = {}
        self._n = 0
        self.fail_get_positions = False
        self.next_fill_mode = "full"  # full|half|zero|reject|unknown

    def place_order(
        self,
        *,
        instrument_key: str,
        side: str,
        quantity: int,
        price: float,
        meta: Optional[Dict[str, Any]] = None,
    ) -> PaperOrder:
        self._n += 1
        oid = f"PAPER-{self._n}"
        mode = self.next_fill_mode
        if mode == "reject":
            order = PaperOrder(oid, instrument_key, side, quantity, 0, 0.0, "REJECTED")
            self.orders[oid] = order
            return order
        if mode == "unknown":
            order = PaperOrder(oid, instrument_key, side, quantity, 0, 0.0, "UNKNOWN")
            self.orders[oid] = order
            return order
        if mode == "zero":
            filled = 0
            status = "OPEN"
        elif mode == "half":
            filled = max(0, quantity // 2)
            status = "PARTIALLY_FILLED" if filled < quantity else "FILLED"
        else:
            filled = quantity
            status = "FILLED"
        order = PaperOrder(oid, instrument_key, side, quantity, filled, float(price), status)
        self.orders[oid] = order
        self._apply_fill(instrument_key, side, filled, price, meta=meta or {})
        return order

    def _apply_fill(
        self,
        instrument_key: str,
        side: str,
        filled: int,
        price: float,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        if filled <= 0:
            return
        meta = meta or {}
        pos = self.positions.get(instrument_key)
        signed = filled if side.upper() == "BUY" else -filled

        if pos is None:
            if signed <= 0:
                return
            self.positions[instrument_key] = {
                "instrument_key": instrument_key,
                "quantity": signed,
                "average_price": float(price),
                "entry_price": float(price),
                "side": "LONG",
                "status": "OPEN",
                "underlying": meta.get("underlying", ""),
                "option_type": meta.get("option_type", ""),
                "strike": meta.get("strike"),
                "expiry": meta.get("expiry", ""),
                "lot_size": int(meta.get("lot_size") or 0),
                "stop_loss": float(meta.get("stop_loss") or 0),
                "target": float(meta.get("target") or 0),
                "trailing_stop": float(meta.get("stop_loss") or 0),
                "initial_stop": float(meta.get("stop_loss") or 0),
                "highest_price": float(price),
                "lowest_price": float(price),
                "mark_price": float(price),
                "entry_time": meta.get("entry_time") or meta.get("timestamp") or "",
                "strategy": meta.get("strategy", "V8_D_PULLBACK_ATM"),
                "trade_id": meta.get("trade_id", ""),
                "unrealized_pnl": 0.0,
                "realized_pnl": 0.0,
                "exit_reason": None,
                "exit_price": None,
                "exit_time": None,
                "closed": False,
            }
            return

        new_qty = int(pos["quantity"]) + signed
        if new_qty == 0:
            # Flatten — mark closed; caller records exit details
            pos["quantity"] = 0
            pos["closed"] = True
            pos["status"] = "CLOSED"
            pos["mark_price"] = float(price)
            self.positions.pop(instrument_key, None)
            return

        # Average up on same-side add
        if (pos["quantity"] > 0 and signed > 0) or (pos["quantity"] < 0 and signed < 0):
            total = abs(pos["quantity"]) * float(pos["average_price"]) + abs(signed) * float(price)
            pos["quantity"] = new_qty
            pos["average_price"] = total / abs(new_qty)
        else:
            pos["quantity"] = new_qty
            pos["average_price"] = float(price)
        self.positions[instrument_key] = pos

    def update_mark(self, instrument_key: str, price: float) -> Optional[Dict[str, Any]]:
        """Update mark-to-market for an open position. Returns position or None."""
        pos = self.positions.get(instrument_key)
        if not pos or pos.get("closed"):
            return None
        px = float(price)
        if px <= 0:
            return pos
        pos["mark_price"] = px
        pos["highest_price"] = max(float(pos.get("highest_price") or px), px)
        pos["lowest_price"] = min(float(pos.get("lowest_price") or px), px)
        qty = int(pos.get("quantity") or 0)
        entry = float(pos.get("entry_price") or pos.get("average_price") or 0)
        if qty > 0:
            pos["unrealized_pnl"] = round((px - entry) * qty, 2)
        elif qty < 0:
            pos["unrealized_pnl"] = round((entry - px) * abs(qty), 2)
        else:
            pos["unrealized_pnl"] = 0.0
        return pos

    def get_positions_with_details(self) -> List[dict]:
        if self.fail_get_positions:
            raise RuntimeError("paper_broker_unavailable")
        out = []
        for k, v in self.positions.items():
            if v.get("closed") or int(v.get("quantity") or 0) == 0:
                continue
            out.append(
                {
                    "instrument_key": k,
                    "quantity": v["quantity"],
                    "average_price": v["average_price"],
                    "entry_price": v.get("entry_price", v["average_price"]),
                    "mark_price": v.get("mark_price", v["average_price"]),
                    "unrealized_pnl": v.get("unrealized_pnl", 0.0),
                    "stop_loss": v.get("stop_loss"),
                    "target": v.get("target"),
                    "trailing_stop": v.get("trailing_stop"),
                    "underlying": v.get("underlying"),
                    "option_type": v.get("option_type"),
                    "strike": v.get("strike"),
                    "expiry": v.get("expiry"),
                    "lot_size": v.get("lot_size"),
                    "status": v.get("status", "OPEN"),
                }
            )
        return out

    def close_all(self) -> None:
        self.positions.clear()
