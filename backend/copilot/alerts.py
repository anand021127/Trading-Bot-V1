"""Structured alerts, fired only on STATE CHANGES (not every candle/tick).
Holds the last-seen state in memory per symbol/component; a caller (e.g.
a scheduled task) calls `check_and_emit(...)` periodically and only gets
an alert back when something actually changed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

ALERT_ICONS = {
    "TRADE_OPPORTUNITY": "\U0001F7E2",
    "NO_TRADE": "\U0001F534",
    "VOLATILITY": "\u26A0\uFE0F",
    "BOT_PROBLEM": "\u26A0\uFE0F",
    "WAITING_CONFIRMATION": "\U0001F7E1",
    "POSITION_UPDATE": "\U0001F535",
}


@dataclass
class Alert:
    kind: str
    message: str

    def formatted(self) -> str:
        return f"{ALERT_ICONS.get(self.kind, '')} {self.message}".strip()


class AlertStateTracker:
    """One instance per running process. Tracks the last decision/status
    per key so alerts only fire on a real transition."""

    def __init__(self) -> None:
        self._last_decision: Dict[str, str] = {}
        self._last_bot_status: Optional[str] = None

    def check_trade_decision(self, symbol: str, decision: str, reason: str) -> Optional[Alert]:
        prev = self._last_decision.get(symbol)
        self._last_decision[symbol] = decision
        if decision == prev:
            return None  # no change -> no alert, avoids spamming every evaluation
        if decision == "TRADE":
            return Alert("TRADE_OPPORTUNITY", f"{symbol}: TRADE — {reason}")
        if decision == "SKIP" and prev == "TRADE":
            return Alert("NO_TRADE", f"{symbol}: opportunity closed — {reason}")
        if decision == "WAIT":
            return Alert("WAITING_CONFIRMATION", f"{symbol}: waiting — {reason}")
        return None

    def check_bot_health(self, overall_status: str, detail: str) -> Optional[Alert]:
        prev = self._last_bot_status
        self._last_bot_status = overall_status
        if overall_status == prev:
            return None
        if overall_status in ("FAILED", "DEGRADED"):
            return Alert("BOT_PROBLEM", f"Bot health is now {overall_status}: {detail}")
        return None

    def position_update(self, symbol: str, note: str) -> Alert:
        # Position updates are always caller-triggered (e.g. on a fill or
        # exit event), not polled, so no dedup needed here.
        return Alert("POSITION_UPDATE", f"{symbol}: {note}")
