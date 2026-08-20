"""Choppiness Index indicator.

CI = 100 × log10(sum_ATR_n / (Highest_High_n - Lowest_Low_n)) / log10(n)
CI < 38.2  → Strong trend
38.2–61.8  → Mixed
CI > 61.8  → Choppy market — SKIP all trades
"""
from __future__ import annotations

import math
from typing import List


def choppiness_index(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14,
) -> List[float]:
    """Calculate the Choppiness Index using the standard log10 formula."""
    if period <= 0:
        raise ValueError("period must be positive")
    if not highs or len(highs) != len(lows) or len(highs) != len(closes):
        return []
    if len(closes) < period + 1:
        return []

    result: List[float] = []
    log_n = math.log10(period)

    for i in range(period, len(closes)):
        window_h = highs[i - period + 1 : i + 1]
        window_l = lows[i - period + 1 : i + 1]
        window_c = closes[i - period : i + 1]  # one extra for TR calc

        # Sum of true ranges over period
        atr_sum = 0.0
        for j in range(1, len(window_c)):
            tr = max(
                window_c[j] - window_l[j - 1],  # approx — window offset
                abs(window_h[j - 1] - window_c[j - 1]),
                abs(window_l[j - 1] - window_c[j - 1]),
            )
            atr_sum += tr

        price_range = max(window_h) - min(window_l)
        if price_range <= 0 or atr_sum <= 0:
            result.append(50.0)
            continue

        ci = 100 * math.log10(atr_sum / price_range) / log_n
        result.append(round(min(max(ci, 0), 200), 4))  # clamp

    return result


# Alias
calculate_choppiness = choppiness_index
