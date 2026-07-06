"""Tests for opening range breakout helper."""

from __future__ import annotations

import pytest

from backend.risk.orb_strategy import orb_signal


def test_orb_long() -> None:
    prices = [100, 101, 102, 103, 104, 110]
    assert orb_signal(prices, opening_range=5) == "long"


def test_orb_short() -> None:
    prices = [50, 49, 48, 47, 46, 40]
    assert orb_signal(prices, opening_range=5) == "short"


def test_orb_none() -> None:
    prices = [10, 11, 12, 11, 10, 11]
    assert orb_signal(prices, opening_range=5) is None


def test_orb_insufficient_raises() -> None:
    with pytest.raises(ValueError):
        orb_signal([1, 2, 3], opening_range=5)
