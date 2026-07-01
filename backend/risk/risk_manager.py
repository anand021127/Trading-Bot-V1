"""Risk management utilities.

Provides a minimal `RiskManager` for tracking daily P&L and enforcing a daily
loss limit. This is intentionally small and unit-testable; later steps may
integrate it with position sizing and order execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class RiskManager:
    capital: float
    daily_loss_limit: float = 0.02  # fraction of capital allowed to lose per day
    pnl_today: float = 0.0
    day: date = field(default_factory=date.today)

    def daily_loss_amount(self) -> float:
        return self.capital * self.daily_loss_limit

    def can_open_trade(self, potential_loss: float) -> bool:
        """Return True if opening a trade with `potential_loss` won't breach the daily limit.

        `potential_loss` should be the worst-case loss for the trade (positive number).
        """
        if potential_loss < 0:
            raise ValueError("potential_loss must be non-negative")

        projected = self.pnl_today - potential_loss
        return projected >= -self.daily_loss_amount()

    def record_trade_result(self, pnl: float) -> None:
        """Record the realized P&L for a completed trade (positive for profit).

        This affects `pnl_today` and therefore the ability to open new trades.
        """
        self.pnl_today += pnl

    def reset_for_new_day(self, new_day: Optional[date] = None) -> None:
        """Reset daily counters for a new trading day."""
        self.day = new_day or date.today()
        self.pnl_today = 0.0

    def remaining_loss_capacity(self) -> float:
        """Return remaining allowed loss (positive number)."""
        remaining = self.daily_loss_amount() + self.pnl_today
        # If pnl_today is negative, remaining reduces; if positive, remaining increases
        return max(0.0, remaining)
