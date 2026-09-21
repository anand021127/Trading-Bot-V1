"""In-process paper broker. Confirmed fills only; never invents quantity."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class PaperOrder:
    order_id: str
    instrument_key: str
    side: str
    requested_qty: int
    filled_qty: int
    avg_price: float
    status: str


class PaperBroker:
    def __init__(self) -> None:
        self.orders: Dict[str, PaperOrder] = {}
        self.positions: Dict[str, Dict] = {}
        self._n = 0
        self.fail_get_positions = False
        self.next_fill_mode = "full"  # full|half|zero|reject|unknown

    def place_order(self, *, instrument_key: str, side: str, quantity: int, price: float) -> PaperOrder:
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
        self._apply_fill(instrument_key, side, filled, price)
        return order

    def _apply_fill(self, instrument_key: str, side: str, filled: int, price: float) -> None:
        if filled <= 0:
            return
        pos = self.positions.get(instrument_key) or {"instrument_key": instrument_key, "quantity": 0, "average_price": 0.0}
        signed = filled if side.upper() == "BUY" else -filled
        new_qty = pos["quantity"] + signed
        if new_qty == 0:
            self.positions.pop(instrument_key, None)
            return
        pos["quantity"] = new_qty
        pos["average_price"] = float(price)
        self.positions[instrument_key] = pos

    def get_positions_with_details(self) -> List[dict]:
        if self.fail_get_positions:
            raise RuntimeError("paper_broker_unavailable")
        return [
            {
                "instrument_key": k,
                "quantity": v["quantity"],
                "average_price": v["average_price"],
            }
            for k, v in self.positions.items()
        ]

    def close_all(self) -> None:
        self.positions.clear()
