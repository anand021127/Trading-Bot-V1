"""Opening Range Breakout (ORB) strategy helpers."""

from __future__ import annotations

from typing import List, Optional


def orb_signal(prices: List[float], opening_range: int = 5) -> Optional[str]:
    """Return 'long' if last price breaks above opening range high,
    'short' if it breaks below opening range low, else None.

    `prices` is a list of sequential prices (e.g., close prices). The opening
    range is taken from the first `opening_range` elements.
    """
    if opening_range <= 0:
        raise ValueError("opening_range must be positive")
    if len(prices) <= opening_range:
        raise ValueError("not enough prices to compute opening range")

    opening = prices[:opening_range]
    high = max(opening)
    low = min(opening)
    last = prices[-1]

    if last > high:
        return "long"
    if last < low:
        return "short"
    return None
