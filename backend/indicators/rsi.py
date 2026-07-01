"""Relative Strength Index indicator."""

from __future__ import annotations

from typing import List


def rsi(values: List[float], period: int = 14) -> List[float]:
    """Calculate the RSI for a sequence of values."""
    if period <= 0:
        raise ValueError("period must be positive")
    if not values:
        return []

    gains: List[float] = []
    losses: List[float] = []
    for index in range(1, len(values)):
        change = values[index] - values[index - 1]
        if change >= 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-change)

    if not gains:
        return [50.0] * (len(values) - 1)

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result: List[float] = []

    for index in range(period, len(gains)):
        if avg_loss == 0:
            result.append(100.0)
            continue
        rs = avg_gain / avg_loss
        rsi_value = 100 - (100 / (1 + rs))
        result.append(rsi_value)
        avg_gain = ((avg_gain * (period - 1)) + gains[index]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[index]) / period

    return result
