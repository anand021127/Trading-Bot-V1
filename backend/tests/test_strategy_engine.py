"""Tests for the options-only strategy registry."""
from __future__ import annotations

from backend.strategy.strategy_engine import MultiStrategyEngine


def test_only_option_strategy_is_registered() -> None:
    assert MultiStrategyEngine().enabled_names() == ["OPTION_PREMIUM"]
