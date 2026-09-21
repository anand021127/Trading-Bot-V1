"""Order request and status models."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class OrderStatus(str, Enum):
    NEW = "NEW"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


def normalize_broker_status(raw: Optional[str]) -> OrderStatus:
    if not raw:
        return OrderStatus.UNKNOWN
    key = str(raw).strip().upper().replace(" ", "_")
    mapping = {
        "COMPLETE": OrderStatus.FILLED,
        "COMPLETED": OrderStatus.FILLED,
        "FILLED": OrderStatus.FILLED,
        "PARTIAL": OrderStatus.PARTIALLY_FILLED,
        "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
        "OPEN": OrderStatus.OPEN,
        "TRIGGER_PENDING": OrderStatus.OPEN,
        "PENDING": OrderStatus.SUBMITTED,
        "SUBMITTED": OrderStatus.SUBMITTED,
        "NEW": OrderStatus.NEW,
        "CANCELLED": OrderStatus.CANCELLED,
        "CANCELED": OrderStatus.CANCELLED,
        "REJECTED": OrderStatus.REJECTED,
        "REJECT": OrderStatus.REJECTED,
        "FAILED": OrderStatus.REJECTED,
    }
    return mapping.get(key, OrderStatus.UNKNOWN)


@dataclass
class OrderRequest:
    symbol: str
    side: str
    quantity: int
    price: float = 0.0
    instrument_key: Optional[str] = None
    order_type: str = "MARKET"
    product: Optional[str] = None
    signal_id: Optional[str] = None
    tag: Optional[str] = None
    # Compatibility fields used by TradingEngine / exit path (ignored by broker adapter)
    contract_metadata: Optional[Dict[str, Any]] = None
    underlying_symbol: Optional[str] = None


@dataclass
class Order:
    id: str
    symbol: str
    status: OrderStatus
    filled_quantity: int = 0
    remaining_quantity: int = 0
    quantity: int = 0
    price: float = 0.0
    average_price: float = 0.0
    fill_details: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def average_fill_price(self) -> float:
        return float(self.average_price or self.price or 0.0)
