"""Multi-strategy orchestration.

Runs every enabled strategy against the same symbol and returns all of
their signals — never just the "winning" one — so the scanner can show
exactly what each strategy concluded, including rejections.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.strategy.signal import StrategySignal
from backend.strategy.strategies.base import Strategy
from backend.strategy.strategies.ema_trend import EMATrendStrategy
from backend.strategy.strategies.option_premium import OptionPremiumStrategy
from backend.strategy.strategies.orb_strategy import ORBStrategy

logger = logging.getLogger(__name__)


class MultiStrategyEngine:
    """Registry + runner for all pluggable strategies.

    Usage:
        engine = MultiStrategyEngine()
        signals = engine.evaluate(symbol, candles, context)
        best = engine.best_signal(signals)
    """

    def __init__(self, strategies: Optional[List[Strategy]] = None) -> None:
        self.strategies: List[Strategy] = strategies or [
            EMATrendStrategy(),
            ORBStrategy(),
            OptionPremiumStrategy(),
        ]

    def enabled_names(self) -> List[str]:
        return [s.name for s in self.strategies]

    def evaluate(
        self,
        symbol: str,
        candles: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
        strategy_names: Optional[List[str]] = None,
    ) -> List[StrategySignal]:
        """Run every (or a filtered subset of) enabled strategy against this
        symbol's candles. Always returns one StrategySignal per strategy —
        including NONE signals with their rejection reasons."""
        results: List[StrategySignal] = []
        for strategy in self.strategies:
            if strategy_names and strategy.name not in strategy_names:
                continue
            try:
                results.append(strategy.evaluate(symbol, candles, context))
            except Exception as e:
                logger.exception("Strategy %s failed for %s", strategy.name, symbol)
                fallback = StrategySignal(strategy_name=strategy.name, symbol=symbol)
                fallback.rejected_reasons = [f"Strategy error: {e}"]
                fallback.entry_reason = "NO TRADE — strategy raised an exception"
                results.append(fallback)
        return results

    def check_exits(
        self,
        strategy_name: str,
        position: Dict[str, Any],
        candles: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        for strategy in self.strategies:
            if strategy.name == strategy_name:
                return strategy.check_exit(position, candles, context)
        return None

    @staticmethod
    def best_signal(signals: List[StrategySignal]) -> Optional[StrategySignal]:
        """Highest-confidence actionable (BUY/SELL) signal, or None if every
        strategy rejected the symbol."""
        actionable = [s for s in signals if s.signal != "NONE"]
        if not actionable:
            return None
        return max(actionable, key=lambda s: s.confidence)
