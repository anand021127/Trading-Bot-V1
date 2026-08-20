"""Unit tests for the market universe helpers."""

from __future__ import annotations

from backend.market_data.universe import build_universe


def test_build_universe_filters_to_active_symbols() -> None:
    """The universe builder should keep active instruments and exclude inactive ones."""
    instruments = [
        {"symbol": "NIFTY 50", "active": True},
        {"symbol": "BANKNIFTY", "active": True},
        {"symbol": "SENSEX", "active": False},
    ]

    result = build_universe(instruments)

    assert result == ["NIFTY 50", "BANKNIFTY"]
