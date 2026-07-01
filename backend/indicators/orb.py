"""Opening Range Breakout helpers."""

from __future__ import annotations

from typing import List, Tuple


def orb_levels(highs: List[float], lows: List[float], opens: List[float], period: int = 15) -> Tuple[float, float]:
    """Return the breakout support and resistance levels for the opening range."""
    if period <= 0:
        raise ValueError("period must be positive")
    if not highs or not lows or not opens:
        raise ValueError("price data cannot be empty")
    if len(highs) != len(lows) or len(highs) != len(opens):
        raise ValueError("highs, lows, and opens must have the same length")

    if len(opens) < period:
        raise ValueError("not enough data for the requested period")

    opening_high = max(highs[:period])
    opening_low = min(lows[:period])
    return opening_high, opening_low
