"""Order-related domain models for the trading bot.

These lightweight models describe the shape of orders and execution requests so
later modules can work with a consistent, typed interface.

V21-FINAL changes:
  - OrderRequest now carries `instrument_key` (the actual NSE_FO|xxx key the
    broker must receive) and `contract_metadata` for consistency validation.
  - Order tracks partial fills: requested_quantity vs filled_quantity.
  - Order status is now granular: SUBMITTED / OPEN / PARTIALLY_FILLED /
    FILLED / CANCELLED / REJECTED / FAILED.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


class OrderStatus:
    """Granular order lifecycle states.

    SUBMITTED  — sent to broker, not yet acknowledged
    OPEN       — broker accepted, awaiting fill
    PARTIALLY_FILLED — some quantity filled, remainder open
    FILLED     — fully filled
    CANCELLED  — cancelled before full fill
    REJECTED   — broker rejected the order
    FAILED     — internal error prevented placement
    """
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"

    # Legacy compat
    PENDING = "SUBMITTED"


@dataclass
class Order:
    """Represents a single order submission or fill."""

    id: str
    symbol: str
    side: str
    quantity: int
    price: Optional[float]
    order_type: str = "market"
    status: str = OrderStatus.SUBMITTED
    timestamp: Optional[datetime] = None

    # V21-FINAL: contract-aware fields
    instrument_key: Optional[str] = None

    # V21-FINAL: fill tracking for partial fills
    requested_quantity: Optional[int] = None
    filled_quantity: Optional[int] = None
    remaining_quantity: Optional[int] = None
    average_fill_price: Optional[float] = None

    # V21-FINAL: execution detail
    fill_details: Dict[str, Any] = field(default_factory=dict)
    # fill_details may contain:
    #   requested_price, simulated_price, slippage, fill_timestamp,
    #   bid_at_fill, ask_at_fill, execution_latency_ms

    @property
    def is_fully_filled(self) -> bool:
        return self.status == OrderStatus.FILLED

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            OrderStatus.FILLED, OrderStatus.CANCELLED,
            OrderStatus.REJECTED, OrderStatus.FAILED,
        )


@dataclass
class OrderRequest:
    """Represents an incoming order request from the strategy layer.

    For OPTION_PREMIUM orders the broker must receive the actual option
    contract's instrument_key (e.g. ``NSE_FO|123456``), NOT the underlying
    index symbol. ``symbol`` remains as human-readable metadata only.
    """

    symbol: str
    side: str
    quantity: int
    price: Optional[float] = None
    order_type: str = "market"

    # V21-FINAL: the actual instrument the broker must trade
    instrument_key: Optional[str] = None

    # V21-FINAL: contract metadata for consistency validation
    # Expected keys: option_type, strike, expiry, lot_size, freeze_quantity
    contract_metadata: Optional[Dict[str, Any]] = None

    # V21-FINAL: the underlying symbol for reference/validation
    underlying_symbol: Optional[str] = None
