"""Exit management for live and paper trading."""

from __future__ import annotations

from typing import Dict, List


class ExitManager:
    """Handles stop-loss and profit-target exit decisions."""

    def __init__(self, stop_loss_pct: float = 0.01, take_profit_pct: float = 0.02) -> None:
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct

    def should_exit(self, position: Dict[str, float], current_price: float) -> bool:
        """Return True when the position should be exited."""
        entry_price = position["entry_price"]
        side = position["side"]
        if side == "long":
            return current_price <= entry_price * (1 - self.stop_loss_pct) or current_price >= entry_price * (1 + self.take_profit_pct)
        if side == "short":
            return current_price >= entry_price * (1 + self.stop_loss_pct) or current_price <= entry_price * (1 - self.take_profit_pct)
        return False
