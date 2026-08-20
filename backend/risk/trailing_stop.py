"""Trailing stop utilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class TrailingStop:
    """Simple trailing stop helper for long and short positions.

    - For `long`: stop = max(prev_stop, price * (1 - trailing_pct))
    - For `short`: stop = min(prev_stop, price * (1 + trailing_pct))

    Usage:
        ts = TrailingStop(trailing_pct=0.05, side="long")
        ts.init(entry_price=100.0)
        ts.update(price=110.0)
        stop = ts.get()
    """

    trailing_pct: float
    side: str = "long"
    _stop: Optional[float] = None

    def init(self, entry_price: float) -> float:
        if entry_price <= 0:
            raise ValueError("entry_price must be positive")
        if self.side == "long":
            self._stop = entry_price * (1 - self.trailing_pct)
        elif self.side == "short":
            self._stop = entry_price * (1 + self.trailing_pct)
        else:
            raise ValueError("side must be 'long' or 'short'")
        return self._stop

    def update(self, price: float) -> float:
        if price <= 0:
            raise ValueError("price must be positive")
        if self._stop is None:
            raise RuntimeError("Trailing stop not initialized. Call init() first.")

        if self.side == "long":
            candidate = price * (1 - self.trailing_pct)
            self._stop = max(self._stop, candidate)
        else:  # short
            candidate = price * (1 + self.trailing_pct)
            self._stop = min(self._stop, candidate)

        return self._stop

    def get(self) -> Optional[float]:
        return self._stop
