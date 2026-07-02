"""Tests for the strategy exit manager."""

from __future__ import annotations

from backend.strategy.exit_manager import ExitManager


def test_should_exit_long_at_stop_loss() -> None:
    manager = ExitManager(stop_loss_pct=0.05, take_profit_pct=0.1)
    position = {"entry_price": 100.0, "side": "long"}
    assert manager.should_exit(position, 94.0)


def test_should_exit_long_at_take_profit() -> None:
    manager = ExitManager(stop_loss_pct=0.05, take_profit_pct=0.1)
    position = {"entry_price": 100.0, "side": "long"}
    assert manager.should_exit(position, 110.5)


def test_should_not_exit_long_in_range() -> None:
    manager = ExitManager(stop_loss_pct=0.05, take_profit_pct=0.1)
    position = {"entry_price": 100.0, "side": "long"}
    assert manager.should_exit(position, 105.0) is False
