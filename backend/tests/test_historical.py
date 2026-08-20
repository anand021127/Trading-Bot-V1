"""Unit tests for the historical market data helpers."""

from __future__ import annotations

from backend.market_data.historical import normalize_candles


def test_normalize_candles_converts_raw_payload_to_candles() -> None:
    """The historical helper should normalize raw candle payloads into a simple structure."""
    raw_payload = {
        "data": [
            {"timestamp": "2024-01-01T00:00:00Z", "open": 100.0, "high": 105.0, "low": 98.0, "close": 103.0, "volume": 1200},
            {"timestamp": "2024-01-01T01:00:00Z", "open": 103.0, "high": 107.0, "low": 101.0, "close": 106.0, "volume": 1400},
        ]
    }

    candles = normalize_candles(raw_payload)

    assert len(candles) == 2
    assert candles[0]["close"] == 103.0
    assert candles[1]["volume"] == 1400
