"""Tests for trend_filter."""

from __future__ import annotations

import pytest

from backend.risk.trend_filter import trend_from_prices


def test_uptrend_detected() -> None:
    prices = [1, 1, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    # short_window=3 (last 3: 7,8,9 avg=8), long_window=6 (last 6 avg=(4+5+6+7+8+9)/6=6.5)
    assert trend_from_prices(prices, short_window=3, long_window=6) == "up"


def test_downtrend_detected() -> None:
    prices = [10, 9, 8, 7, 6, 5, 4, 3]
    assert trend_from_prices(prices, short_window=2, long_window=5) == "down"


def test_sideways_detected() -> None:
    prices = [100, 101, 100, 100, 101, 100, 100, 101]
    assert trend_from_prices(prices, short_window=3, long_window=6, tol=0.02) == "sideways"


def test_insufficient_length_raises() -> None:
    with pytest.raises(ValueError):
        trend_from_prices([1, 2, 3], short_window=2, long_window=5)
