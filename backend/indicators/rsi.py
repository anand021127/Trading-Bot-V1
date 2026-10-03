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

    def _rsi_value(a_gain: float, a_loss: float) -> float:
        if a_loss == 0:
            return 100.0
        return round(100 - (100 / (1 + a_gain / a_loss)), 4)

    # First value = RSI of closes[period] (seeded by the SMA of the first
    # `period` changes). Every later value is emitted AFTER folding that bar's
    # change into the Wilder averages, so the LAST element is the RSI of the
    # LAST close — i.e. len(result) == len(values) - period and
    # result[k] is the RSI of values[period + k].
    #
    # BUG FIXED (off-by-one): the previous loop appended the RSI *before*
    # updating the averages with the current change and never emitted the
    # final update, so result[-1] was the RSI of the PREVIOUS candle — every
    # consumer (V8-D's RSI zone, AI context, confidence scoring) read an RSI
    # that lagged the market by one bar. Verified against the textbook Wilder
    # RSI on real NIFTY50 5-minute data (see backend/tests/test_rsi_indicator.py).
    result.append(_rsi_value(avg_gain, avg_loss))
    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period
        result.append(_rsi_value(avg_gain, avg_loss))

    return result


def calculate_rsi(values: Union[List[float], List[int]], period: int = 14) -> List[float]:
    return rsi([float(v) for v in values], period)
