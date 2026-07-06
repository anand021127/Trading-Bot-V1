"""Unit tests for the position sizer module."""

from __future__ import annotations

import pytest

from backend.risk.position_sizer import PositionSizer


def test_calculate_basic_size() -> None:
    sizer = PositionSizer(capital=100_000.0, risk_per_trade=0.01)
    qty = sizer.calculate(entry_price=100.0, stop_loss_price=90.0)
    assert qty == 100


def test_min_qty_enforced() -> None:
    # Use very small capital so the raw calculated quantity is 0
    sizer = PositionSizer(capital=1.0, risk_per_trade=0.01, min_qty=1)
    qty = sizer.calculate(entry_price=100.0, stop_loss_price=99.9)
    assert qty == 1


def test_max_qty_capped() -> None:
    sizer = PositionSizer(capital=100_000.0, risk_per_trade=0.01, max_qty=10)
    qty = sizer.calculate(entry_price=100.0, stop_loss_price=90.0)
    assert qty == 10


def test_zero_risk_raises() -> None:
    sizer = PositionSizer(capital=1000.0, risk_per_trade=0.01)
    with pytest.raises(ValueError):
        sizer.calculate(entry_price=100.0, stop_loss_price=100.0)
