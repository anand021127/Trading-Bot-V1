"""Exponential moving average — production indicator module."""
from __future__ import annotations
from typing import List, Union


def ema(values: List[float], period: int) -> List[float]:
    """Calculate EMA for a list of values."""
    if period <= 0:
        raise ValueError("period must be positive")
    if not values:
        return []
    multiplier = 2 / (period + 1)
    result: List[float] = []
    previous = values[0]
    for value in values:
        previous = (value * multiplier) + (previous * (1 - multiplier))
        result.append(round(previous, 6))
    return result


def ema_slope(ema_values: List[float], lookback: int = 3) -> float:
    """Return slope of EMA over last N bars. Positive = uptrend."""
    if len(ema_values) < lookback + 1:
        return 0.0
    return (ema_values[-1] - ema_values[-1 - lookback]) / lookback


# ── Aliases for diagnostics and strategy code ─────────────────────────────────
def calculate_ema(values: Union[List[float], List[int]], period: int) -> List[float]:
    return ema([float(v) for v in values], period)


# Allow pandas Series if available
try:
    import pandas as pd
    def ema_series(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()
except ImportError:
    pass
