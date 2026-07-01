"""Average True Range indicator."""

from __future__ import annotations

from typing import List


def atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[float]:
    """Calculate the Average True Range using Wilder's smoothing."""
    if period <= 0:
        raise ValueError("period must be positive")
    if not highs or not lows or not closes:
        return []
    if len(highs) != len(lows) or len(highs) != len(closes):
        raise ValueError("highs, lows, and closes must have the same length")

    true_ranges: List[float] = []
    for index in range(1, len(closes)):
        prev_close = closes[index - 1]
        tr = max(highs[index] - lows[index], abs(highs[index] - prev_close), abs(lows[index] - prev_close))
        true_ranges.append(tr)

    if not true_ranges:
        return []

    first_value = sum(true_ranges[:period]) / period
    result = [first_value]
    previous = first_value

    for value in true_ranges[period:]:
        previous = ((period - 1) * previous + value) / period
        result.append(previous)

    return result
