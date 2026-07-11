"""Shared strategy-signal contract used by every strategy in the engine.

Every strategy — EMA Trend, ORB, Option Premium — returns exactly this shape
so the scanner, the dashboard, and the execution pipeline can treat them
uniformly. Nothing in here fabricates a signal: `NONE` with a populated
`rejected_reasons` list is the expected, common result.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class SignalType:
    BUY = "BUY"
    SELL = "SELL"
    NONE = "NONE"


@dataclass
class StrategySignal:
    strategy_name: str
    symbol: str
    signal: str = SignalType.NONE

    # 0-100. This is a *transparency* score (how many of the strategy's own
    # conditions passed), not a probability of profit.
    confidence: float = 0.0

    # Human-readable "why" — always populated, even on NONE, so the scanner
    # can show it directly (e.g. "EMA PASS, RSI PASS, VOLUME FAILED").
    entry_reason: str = ""
    exit_reason: str = ""

    entry_price: float = 0.0
    stop_loss: float = 0.0
    target: float = 0.0

    # condition_name -> passed/failed, in the order they were checked.
    conditions: Dict[str, bool] = field(default_factory=dict)
    # Every reason a trade was NOT taken, even if only one condition failed —
    # per the "never hide rejected signals" requirement.
    rejected_reasons: List[str] = field(default_factory=list)

    # Raw indicator values for display (RSI, ATR, EMA20/50, volume ratio,...)
    indicators: Dict[str, Any] = field(default_factory=dict)

    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def conditions_passed(self) -> int:
        return sum(1 for v in self.conditions.values() if v)

    @property
    def conditions_total(self) -> int:
        return len(self.conditions)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "symbol": self.symbol,
            "signal": self.signal,
            "confidence": round(self.confidence, 1),
            "entry_reason": self.entry_reason,
            "exit_reason": self.exit_reason,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "target": self.target,
            "conditions": self.conditions,
            "conditions_passed": self.conditions_passed,
            "conditions_total": self.conditions_total,
            "rejected_reasons": self.rejected_reasons,
            "indicators": self.indicators,
            "generated_at": self.generated_at,
        }


def build_condition_summary(conditions: Dict[str, bool]) -> str:
    """'EMA_TREND PASS, RSI PASS, VOLUME FAILED' style summary line."""
    return ", ".join(
        f"{name.upper()} {'PASS' if passed else 'FAILED'}"
        for name, passed in conditions.items()
    )


def rejected_reasons_from(conditions: Dict[str, bool], reason_text: Dict[str, str]) -> List[str]:
    """Turn failed conditions into full human-readable rejection reasons."""
    return [reason_text[name] for name, passed in conditions.items() if not passed and name in reason_text]
