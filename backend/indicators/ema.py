"""Exponential moving average helpers."""

from __future__ import annotations

from typing import List


def ema(values: List[float], period: int) -> List[float]:
    """Calculate an exponential moving average for a list of values."""
    if period <= 0:
        raise ValueError("period must be positive")
    if not values:
        return []

    multiplier = 2 / (period + 1)
    result: List[float] = []
    previous = values[0]

    for value in values:
        previous = (value * multiplier) + (previous * (1 - multiplier))
        result.append(previous)

    return result
