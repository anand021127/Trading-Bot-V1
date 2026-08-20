"""Volume Weighted Average Price — production indicator module."""
from __future__ import annotations

from typing import List, Union


def vwap(highs: List[float], lows: List[float], closes: List[float],
         volumes: List[float]) -> List[float]:
    """Session VWAP, recomputed cumulatively from the first bar in the input.

    Caller is responsible for slicing `highs`/`lows`/`closes`/`volumes` down
    to a single trading session (VWAP resets every day) — this function does
    not know about calendar/session boundaries.
    """
    n = len(closes)
    if n == 0 or len(highs) != n or len(lows) != n or len(volumes) != n:
        return []

    result: List[float] = []
    cum_pv = 0.0
    cum_vol = 0.0
    for i in range(n):
        typical_price = (highs[i] + lows[i] + closes[i]) / 3.0
        vol = float(volumes[i])
        cum_pv += typical_price * vol
        cum_vol += vol
        result.append(round(cum_pv / cum_vol, 4) if cum_vol > 0 else round(typical_price, 4))
    return result


def calculate_vwap(
    highs: Union[List[float], List[int]],
    lows: Union[List[float], List[int]],
    closes: Union[List[float], List[int]],
    volumes: Union[List[float], List[int]],
) -> List[float]:
    return vwap(
        [float(v) for v in highs],
        [float(v) for v in lows],
        [float(v) for v in closes],
        [float(v) for v in volumes],
    )
