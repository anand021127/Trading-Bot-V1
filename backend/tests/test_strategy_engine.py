"""Tests for strategy registry / MultiStrategyEngine selection."""
from __future__ import annotations

from backend.config.strategy_registry import load_strategies
from backend.strategy.strategy_engine import MultiStrategyEngine


def test_empty_default_engine_has_no_silent_option_premium() -> None:
    assert MultiStrategyEngine().enabled_names() == []


def test_explicit_v8d_only() -> None:
    eng = MultiStrategyEngine(strategies=load_strategies(["V8_D_PULLBACK_ATM"]))
    assert eng.enabled_names() == ["V8_D_PULLBACK_ATM"]
    assert "OPTION_PREMIUM" not in eng.enabled_names()
