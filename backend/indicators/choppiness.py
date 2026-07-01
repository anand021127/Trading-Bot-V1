"""Choppiness Index indicator."""

from __future__ import annotations

from typing import List


def choppiness_index(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[float]:
    """Calculate the Choppiness Index for the provided price series."""
    if period <= 0:
        raise ValueError("period must be positive")
    if not highs or not lows or not closes:
        return []
    if len(highs) != len(lows) or len(highs) != len(closes):
        raise ValueError("highs, lows, and closes must have the same length")

    result: List[float] = []
    for index in range(period - 1, len(closes)):
        window_high = max(highs[index - period + 1 : index + 1])
        window_low = min(lows[index - period + 1 : index + 1])
        sum_trend = sum(closes[index - period + 1 : index + 1])
        if sum_trend == 0:
            result.append(0.0)
            continue
        value = 100 * (window_high - window_low) / sum_trend
        result.append(value)

    return result
