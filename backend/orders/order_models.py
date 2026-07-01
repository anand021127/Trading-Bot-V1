"""Order-related domain models for the trading bot.

These lightweight models describe the shape of orders and execution requests so
later modules can work with a consistent, typed interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Order:
    """Represents a single order submission or fill."""

    id: str
    symbol: str
    side: str
    quantity: int
    price: Optional[float]
    order_type: str = "market"
    status: str = "pending"
    timestamp: Optional[datetime] = None


@dataclass
class OrderRequest:
    """Represents an incoming order request from the strategy layer."""

    symbol: str
    side: str
    quantity: int
    price: Optional[float] = None
    order_type: str = "market"
