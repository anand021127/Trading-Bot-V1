"""Base class every strategy in the multi-strategy engine implements."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from backend.strategy.signal import StrategySignal


class Strategy(ABC):
    """A strategy turns a candle series (+ optional context) into exactly
    one `StrategySignal`. It never raises on bad/short data — it returns a
    NONE signal with `rejected_reasons` explaining why."""

    name: str = "BASE"
    min_candles: int = 30

    @abstractmethod
    def evaluate(
        self,
        symbol: str,
        candles: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
    ) -> StrategySignal:
        """`candles` — ascending-time OHLCV dicts with open/high/low/close/
        volume/timestamp keys. `context` — optional extras a strategy may
        use (e.g. {'index_trend': 'BULLISH'} for ORB, or an option chain for
        the premium strategy)."""
        raise NotImplementedError

    def check_exit(
        self,
        position: Dict[str, Any],
        candles: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Return an exit reason string if the position should be closed,
        else None. Default: no strategy-specific exit (ATR/SL/target-based
        exits are handled by the shared ExitManager)."""
        return None

    def _insufficient_data(self, symbol: str, have: int) -> StrategySignal:
        sig = StrategySignal(strategy_name=self.name, symbol=symbol)
        sig.rejected_reasons = [
            f"Insufficient candle data: have {have}, need at least {self.min_candles}"
        ]
        sig.entry_reason = "NO TRADE — insufficient data"
        return sig
