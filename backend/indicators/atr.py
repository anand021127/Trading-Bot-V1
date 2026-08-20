"""Average True Range — production indicator module."""
from __future__ import annotations
from typing import List, Union


def atr(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14,
) -> List[float]:
    """Calculate ATR using Wilder's smoothing."""
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(closes)
    if n < 2 or len(highs) != n or len(lows) != n:
        return []

    true_ranges: List[float] = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return []

    first = sum(true_ranges[:period]) / period
    result = [round(first, 6)]
    prev = first
    for tr in true_ranges[period:]:
        prev = ((period - 1) * prev + tr) / period
        result.append(round(prev, 6))
    return result


def atr_percentile_rank(atr_values: List[float], lookback: int = 50) -> float:
    """Return 0-1: where current ATR sits in its recent history. < 0.2 = very low vol."""
    if len(atr_values) < 2:
        return 0.5
    window = atr_values[-lookback:]
    current = window[-1]
    low, high = min(window), max(window)
    if high == low:
        return 0.5
    return (current - low) / (high - low)


def calculate_atr(
    highs: Union[List[float], List[int]],
    lows: Union[List[float], List[int]],
    closes: Union[List[float], List[int]],
    period: int = 14,
) -> List[float]:
    return atr(
        [float(v) for v in highs],
        [float(v) for v in lows],
        [float(v) for v in closes],
        period,
    )
