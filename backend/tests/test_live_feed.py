"""Unit tests for the live market data feed helper."""

from __future__ import annotations

from backend.market_data.live_feed import normalize_quote


def test_normalize_quote_extracts_latest_price_and_symbol() -> None:
    """The live feed helper should expose the latest price and symbol from a quote payload."""
    payload = {
        "data": {
            "symbol": "NIFTY 50",
            "last_price": 22100.0,
            "change": 120.0,
        }
    }

    result = normalize_quote(payload)

    assert result["symbol"] == "NIFTY 50"
    assert result["last_price"] == 22100.0
    assert result["change"] == 120.0
