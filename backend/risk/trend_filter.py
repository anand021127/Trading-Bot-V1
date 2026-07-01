"""Trend filtering utilities."""

from __future__ import annotations

from typing import List


def trend_from_prices(prices: List[float], short_window: int = 5, long_window: int = 20, tol: float = 0.0) -> str:
    """Return 'up', 'down', or 'sideways' based on moving average comparison.

    - Uses simple arithmetic mean for windows.
    - `tol` is fractional tolerance: e.g., 0.01 means 1% buffer to avoid noise.
    """
    if short_window <= 0 or long_window <= 0:
        raise ValueError("window sizes must be positive")
    if short_window >= long_window:
        raise ValueError("short_window must be less than long_window")
    if len(prices) < long_window:
        raise ValueError("not enough prices to compute long-window average")

    short_ma = sum(prices[-short_window:]) / short_window
    long_ma = sum(prices[-long_window:]) / long_window

    if short_ma > long_ma * (1 + tol):
        return "up"
    if short_ma < long_ma * (1 - tol):
        return "down"
    return "sideways"
