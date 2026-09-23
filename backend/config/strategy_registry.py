"""Explicit strategy selection — no silent fallback."""
from __future__ import annotations

import logging
from typing import Dict, Type

from backend.strategy.strategies.base import Strategy
from backend.strategy.strategies.option_premium import OptionPremiumStrategy
from backend.strategy.strategies.v8d_strategy import V8DStrategy

logger = logging.getLogger(__name__)

REGISTERED_STRATEGIES: Dict[str, Type[Strategy]] = {
    "V8_D_PULLBACK_ATM": V8DStrategy,
    "OPTION_PREMIUM": OptionPremiumStrategy,
}


class StrategySelectionError(RuntimeError):
    pass


def load_strategy(name: str) -> Strategy:
    if not name or not str(name).strip():
        raise StrategySelectionError(
            "TRADING_STRATEGY is empty. Set it explicitly (e.g. V8_D_PULLBACK_ATM). "
            "Refusing silent fallback."
        )
    key = str(name).strip()
    cls = REGISTERED_STRATEGIES.get(key)
    if cls is None:
        raise StrategySelectionError(
            f"Unknown strategy '{key}'. Known: {sorted(REGISTERED_STRATEGIES)}. "
            "No fallback will be applied."
        )
    strategy = cls()
    logger.info("ACTIVE_STRATEGY=%s class=%s", strategy.name, cls.__name__)
    return strategy


def load_strategies(names: list) -> list:
    """Instantiate exactly the requested strategies — no default, no silent add-ons."""
    if not names:
        raise StrategySelectionError(
            "No strategy names provided. Set strategies explicitly "
            "(e.g. [\"V8_D_PULLBACK_ATM\"]). Refusing silent OPTION_PREMIUM fallback."
        )
    out = []
    for n in names:
        out.append(load_strategy(n))
    return out
