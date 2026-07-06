"""Unit tests for the indicator helpers."""

from __future__ import annotations

from backend.indicators.atr import atr
from backend.indicators.choppiness import choppiness_index
from backend.indicators.ema import ema
from backend.indicators.orb import orb_levels
from backend.indicators.rsi import rsi
from backend.indicators.volume import volume_spike


def test_ema_returns_expected_series() -> None:
    """EMA should produce a smooth series from the input values."""
    values = [10.0, 12.0, 14.0, 16.0]
    result = ema(values, 2)

    assert len(result) == len(values)
    assert result[0] > 0
    assert result[-1] > result[0]


def test_atr_returns_values_for_price_series() -> None:
    """ATR should return a non-empty series for valid price data."""
    result = atr([100, 102, 101], [95, 96, 97], [98, 100, 99], period=2)

    assert len(result) > 0
    assert result[0] > 0


def test_rsi_returns_values_for_price_series() -> None:
    """RSI should return a series for the input values."""
    result = rsi([50, 52, 49, 55], period=2)

    assert len(result) > 0


def test_volume_spike_detects_high_volume() -> None:
    """Volume spikes should be detected when volumes exceed the average threshold."""
    result = volume_spike([10, 20, 30], threshold=1.5)

    assert result == [False, False, True]


def test_choppiness_index_returns_values() -> None:
    """The Choppiness Index should produce a numeric series."""
    result = choppiness_index([100, 102, 98, 101], [95, 97, 94, 99], [98, 100, 96, 100], period=2)

    assert len(result) > 0
    assert all(value >= 0 for value in result)


def test_orb_levels_return_opening_range() -> None:
    """ORB levels should reflect the opening range high and low."""
    high, low = orb_levels([100, 102, 101], [98, 99, 100], [99, 100, 101], period=2)

    assert high == 102.0
    assert low == 98.0
