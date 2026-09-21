"""Persistence models."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class Trade:
    id: str
    symbol: str
    side: str
    quantity: int
    price: float
    timestamp: datetime
    strategy: str = ""
    status: str = "open"
    pnl: Optional[float] = None
    notes: str = ""


@dataclass
class Position:
    symbol: str
    quantity: int
    average_price: float
    entry_time: datetime
    instrument_key: str = ""
    side: str = "long"
    unrealized_pnl: float = 0.0
    extra: dict = field(default_factory=dict)
