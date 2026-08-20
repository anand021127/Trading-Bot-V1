"""Unit tests for `RiskManager`."""

from __future__ import annotations

from datetime import date

import pytest

from backend.risk.risk_manager import RiskManager


def test_record_and_remaining_capacity() -> None:
    rm = RiskManager(capital=100_000.0, daily_loss_limit=0.01)
    assert rm.daily_loss_amount() == 1000.0
    assert rm.remaining_loss_capacity() == 1000.0

    rm.record_trade_result(-250.0)
    assert rm.pnl_today == -250.0
    assert rm.remaining_loss_capacity() == 750.0

    rm.record_trade_result(100.0)
    assert rm.pnl_today == -150.0
    assert rm.remaining_loss_capacity() == 850.0


def test_can_open_trade_logic() -> None:
    rm = RiskManager(capital=50_000.0, daily_loss_limit=0.02)
    # daily limit = 1000
    assert rm.can_open_trade(500.0) is True

    rm.record_trade_result(-900.0)
    # pnl_today = -900, remaining capacity = 100
    assert rm.can_open_trade(200.0) is False
    assert rm.can_open_trade(100.0) is True


def test_reset_for_new_day() -> None:
    rm = RiskManager(capital=10_000.0, daily_loss_limit=0.05)
    rm.record_trade_result(-200.0)
    assert rm.pnl_today < 0
    new_day = date(2000, 1, 1)
    rm.reset_for_new_day(new_day=new_day)
    assert rm.pnl_today == 0.0
    assert rm.day == new_day


def test_potential_loss_negative_raises() -> None:
    rm = RiskManager(capital=1000.0)
    with pytest.raises(ValueError):
        rm.can_open_trade(-10.0)
