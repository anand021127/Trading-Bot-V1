"""Copilot grounding guard (PHASE 5 items 25 & 27).

The deterministic context is authoritative. A provider answer that
CONTRADICTS it must never reach the operator. This module detects the
previously observed failure classes:

  1. CONTEXT-SAYS-ZERO / ANSWER-SAYS-ACTIVE:
     context reports trades_today=0, open_positions=0, realized_pnl=0 while
     the answer claims the bot is executing trades / holding positions /
     booking profit.
  2. FALSE NO-DATA DENIAL:
     the answer claims "no real-time access / I don't have current data"
     although relevant live data WAS included in the resolved context.
  3. EXECUTION-AUTHORITY CLAIM:
     the answer claims it placed/can place orders or changed settings.

`check_grounding(answer, context)` returns violations; the caller (chat
jobs) DISCARDS the answer when violations exist and serves the verified
deterministic explanation instead. Freshness is validated against SOURCE
timestamps (candle/chain timestamps in the context), never datetime.now().
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ── claim patterns ────────────────────────────────────────────────────
_ACTIVE_TRADE_RE = re.compile(
    r"\b(bot is|currently)?\s*(executing|placing|opening|entering|taking|bought|sold|purchased)\s+"
    r"(a\s+)?(trade|trades|position|positions|order|orders|entry|entries)\b",
    re.I,
)
_HOLDING_POSITION_RE = re.compile(
    r"\b(holding|has|have|currently in|currently holding)\s+(a\s+)?(open\s+)?(position|positions)\b",
    re.I,
)
_PNL_CLAIM_RE = re.compile(
    r"\b(realized|booked|made|earned|profited)\s+(a\s+)?(profit|₹\s?[\d,]+|rs\.?\s?[\d,]+)\b",
    re.I,
)
_NO_DATA_RE = re.compile(
    r"(don't|do not|can't|cannot|no)\s+(have\s+)?(real[- ]time|live|current|realtime)\s*"
    r"(access|data|market data|information)|"
    r"\bi (don't|do not) have (access|data)\b|"
    r"\bno (real[- ]time|live|current) (data|access|information)\b",
    re.I,
)
_EXEC_AUTHORITY_RE = re.compile(
    r"\b(i|I've|I have|I'll|I will|i am|I'm)\s+(can\s+|will\s+|\s*)?(placed|placing|place|"
    r"executed|executing|cancelled|modify|modified|changed|closed)\s+"
    r"(the\s+|a\s+|an\s+)?(order|orders|trade|trades|position|positions|settings?|risk|strategy)\b",
    re.I,
)


def _zero_state_facts(context: Dict[str, Any]) -> Dict[str, bool]:
    """Extract deterministic zero-state facts from the resolved context."""
    facts: Dict[str, bool] = {}
    bot = context.get("bot_health") or {}
    if isinstance(bot, dict) and bot.get("available"):
        # explicit zero counters are only trustworthy when health data resolved
        facts["checked"] = True
    daily = context.get("daily_pnl") or {}
    if isinstance(daily, dict) and daily.get("available"):
        trades_today = daily.get("trades_today")
        realized = daily.get("realized_pnl")
        if trades_today == 0:
            facts["trades_today_zero"] = True
        if realized == 0 or realized == 0.0:
            facts["realized_zero"] = True
    positions = context.get("open_positions") or {}
    if isinstance(positions, dict) and positions.get("available"):
        plist = positions.get("positions") or []
        if isinstance(plist, list) and len(plist) == 0:
            facts["positions_zero"] = True
    return facts


def check_grounding(answer: str, context: Dict[str, Any]) -> List[Dict[str, str]]:
    """Return a list of grounding violations in `answer` vs `context`.

    Empty list == grounded. Violations carry machine-readable `type` and a
    human `detail` for the deterministic fallback message.
    """
    violations: List[Dict[str, str]] = []
    if not answer or not isinstance(answer, str):
        return violations
    facts = _zero_state_facts(context)
    a = answer

    # 1. zero-state vs active-trade claims
    if facts.get("trades_today_zero") or facts.get("positions_zero"):
        if _ACTIVE_TRADE_RE.search(a) or _HOLDING_POSITION_RE.search(a):
            violations.append({
                "type": "FABRICATED_ACTIVITY",
                "detail": "Answer claims trades/positions while deterministic context reports none.",
            })
    if facts.get("realized_zero") and _PNL_CLAIM_RE.search(a):
        violations.append({
            "type": "FABRICATED_PNL",
            "detail": "Answer claims realized profit while context reports zero realized P&L.",
        })

    # 2. false no-data denial: only when live-ish data was actually provided
    if _NO_DATA_RE.search(a):
        has_live_data = any(
            isinstance(context.get(k), dict) and context[k].get("available")
            for k in ("market_status", "bot_health", "open_positions", "daily_pnl", "analysis")
        )
        if has_live_data:
            violations.append({
                "type": "FALSE_NO_DATA_DENIAL",
                "detail": "Answer denies having live data while resolved context contains it.",
            })

    # 3. execution authority claim
    if _EXEC_AUTHORITY_RE.search(a):
        violations.append({
            "type": "EXECUTION_AUTHORITY_CLAIM",
            "detail": "Answer claims order/setting authority — the Copilot is observation-only.",
        })
    return violations


def deterministic_fallback(question: str, context: Dict[str, Any], violations: List[Dict[str, str]]) -> str:
    """Verified deterministic explanation served INSTEAD of a contradicting
    LLM answer. Every number comes from the resolved context — nothing is
    invented."""
    lines = ["(The AI answer was discarded because it contradicted verified bot state.)", ""]
    daily = context.get("daily_pnl") or {}
    positions = context.get("open_positions") or {}
    bot = context.get("bot_health") or {}
    if isinstance(daily, dict) and daily.get("available"):
        lines.append(
            f"Today: {daily.get('trades_today', 0)} trade(s), "
            f"realized P&L ₹{float(daily.get('realized_pnl', 0) or 0):.2f}."
        )
    if isinstance(positions, dict) and positions.get("available"):
        plist = positions.get("positions") or []
        if not plist:
            lines.append("Open positions: none.")
        else:
            for p in plist:
                lines.append(f"- {p.get('symbol', '?')}: qty {p.get('quantity', '?')} @ ₹{p.get('average_price', '?')}")
    if isinstance(bot, dict) and bot.get("available"):
        overall = bot.get("overall_status") or bot.get("status") or "unknown"
        lines.append(f"Bot health: {overall}.")
    if not context.get("market_status", {}).get("available", True) if isinstance(context.get("market_status"), dict) else False:
        ms = context.get("market_status") or {}
        lines.append(f"Market data unavailable: {ms.get('reason', 'unknown')}")
    lines.append("")
    lines.append("The Copilot is observation-only: it never places orders or changes settings.")
    return "\n".join(lines)


def validate_context_freshness(context: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Freshness uses SOURCE timestamps from the context (candle_timestamp /
    data_age_seconds), never wall-clock substitution.

    Returns (ok, reason). ok=False means the context itself carries stale or
    missing source timestamps for live sections — the answer must state that
    staleness rather than present data as live.
    """
    analysis = context.get("analysis") or {}
    if isinstance(analysis, dict) and analysis.get("available", True):
        status = analysis.get("data_status")
        if status == "STALE":
            return False, f"analysis data is STALE (age {analysis.get('data_age_seconds')}s, ts {analysis.get('candle_timestamp')})"
    market = context.get("market_status") or {}
    if isinstance(market, dict) and market.get("available"):
        ts = market.get("timestamp") or market.get("as_of")
        if ts:
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                _ = dt  # parseable — source timestamp present
            except Exception:
                return False, f"unparseable market timestamp: {ts!r}"
    return True, None


__all__ = [
    "check_grounding",
    "deterministic_fallback",
    "validate_context_freshness",
]
