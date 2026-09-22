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


@dataclass
class PerformanceSnapshot:
    """Daily performance aggregate used by the performance router."""
    date: str
    net_pnl: float = 0.0
    trades_count: int = 0
    win_rate: float = 0.0
    equity: float = 0.0
