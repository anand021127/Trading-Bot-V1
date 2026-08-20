"""Volume-based helpers for the trading bot."""

from __future__ import annotations

from typing import List


def volume_spike(volumes: List[float], threshold: float = 2.0) -> List[bool]:
    """Return whether each volume is above the average of the other values times a threshold."""
    if not volumes:
        return []

    results: List[bool] = []
    for index, volume in enumerate(volumes):
        others = volumes[:index] + volumes[index + 1 :]
        if not others:
            results.append(False)
            continue

        average = sum(others) / len(others)
        results.append(volume > average * threshold)

    return results

# Alias
def calculate_volume_ratio(volume: list, lookback: int = 20) -> list:
    if len(volume) < lookback:
        return []
    result = []
    for i in range(lookback, len(volume)):
        avg = sum(volume[i - lookback:i]) / lookback
        result.append(volume[i] / avg if avg > 0 else 1.0)
    return result
