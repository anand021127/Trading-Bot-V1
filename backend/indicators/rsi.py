"""Relative Strength Index — production indicator module."""
from __future__ import annotations
from typing import List, Union


def rsi(values: List[float], period: int = 14) -> List[float]:
    """Calculate RSI for a sequence of values. Returns values after warmup."""
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period + 1:
        return []

    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    if len(gains) < period:
        return []

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result: List[float] = []

    for i in range(period, len(gains)):
        if avg_loss == 0:
            result.append(100.0)
        else:
            rs = avg_gain / avg_loss
            result.append(round(100 - (100 / (1 + rs)), 4))
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    return result


def calculate_rsi(values: Union[List[float], List[int]], period: int = 14) -> List[float]:
    return rsi([float(v) for v in values], period)
