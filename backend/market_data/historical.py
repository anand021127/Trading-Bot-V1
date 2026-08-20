"""Helpers for historical market data retrieval and normalization.

The module exposes a simple normalization function that translates broker-style
payloads into a consistent candle representation for the strategy layer.
"""

from __future__ import annotations

from typing import Any, Dict, List


def normalize_candles(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalize a raw historical candle payload into a list of dictionaries."""
    raw_candles = payload.get("data", [])
    normalized: List[Dict[str, Any]] = []

    for candle in raw_candles:
        normalized.append(
            {
                "timestamp": candle.get("timestamp"),
                "open": candle.get("open"),
                "high": candle.get("high"),
                "low": candle.get("low"),
                "close": candle.get("close"),
                "volume": candle.get("volume"),
            }
        )

    return normalized
