"""Position sizing helpers.

This module contains utilities to calculate how many units to trade given
capital and per-trade risk constraints.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class PositionSizer:
    """Helper to calculate position sizes.

    Attributes
    ----------
    capital: float
        Total capital available for sizing calculations.
    risk_per_trade: float
        Fraction of capital to risk per trade (e.g., 0.01 for 1%).
    min_qty: int
        Minimum quantity to return when calculation yields zero.
    max_qty: Optional[int]
        Optional hard cap on returned quantity.
    """

    capital: float
    risk_per_trade: float = 0.01
    min_qty: int = 1
    max_qty: Optional[int] = None

    def calculate(
        self, *, entry_price: float, stop_loss_price: float
    ) -> int:
        """Calculate the integer quantity to trade.

        The sizing rules:
        - risk_amount = capital * risk_per_trade
        - per_unit_risk = abs(entry_price - stop_loss_price)
        - raw_qty = floor(risk_amount / per_unit_risk)
        - enforce `min_qty` and `max_qty` if provided

        Raises
        ------
        ValueError
            If `per_unit_risk` is zero or negative, or inputs are invalid.
        """
        if entry_price <= 0 or stop_loss_price < 0:
            raise ValueError("Prices must be non-negative and entry > 0")

        per_unit_risk = abs(entry_price - stop_loss_price)
        if per_unit_risk <= 0:
            raise ValueError("Stop-loss must differ from entry price to compute risk")

        risk_amount = self.capital * self.risk_per_trade
        raw_qty = int(risk_amount // per_unit_risk)

        qty = max(raw_qty, self.min_qty)
        if self.max_qty is not None:
            qty = min(qty, self.max_qty)

        return int(qty)
