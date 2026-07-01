"""Database models for the trading bot.

The models are lightweight dataclasses that describe the persisted entities the
application will work with. They are intentionally simple so they can be used
by the database manager and tests without tying the code to a specific ORM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Trade:
    """Represents a single trade executed by the bot."""

    id: str
    symbol: str
    side: str
    quantity: int
    price: float
    timestamp: datetime
    strategy: str = ""
    status: str = "filled"
    pnl: Optional[float] = None
    notes: str = ""


@dataclass
class Position:
    """Represents an open position tracked in the portfolio."""

    symbol: str
    quantity: int
    average_price: float
    entry_time: datetime
    side: str = "long"
    unrealized_pnl: float = 0.0


@dataclass
class PerformanceSnapshot:
    """Represents a performance summary stored for analytics."""

    date: str
    net_pnl: float
    trades_count: int
    win_rate: float
    equity: float
    created_at: datetime = field(default_factory=datetime.utcnow)
