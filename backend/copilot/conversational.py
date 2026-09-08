"""Conversational Copilot interface.

Routing is deterministic keyword matching, not LLM-driven — this is a
deliberate simplification vs. full LLM function-calling: it guarantees
(by construction, not by prompting) that every current-state question
resolves through a real tool call before any explanation is generated,
satisfying "AI should never hallucinate live information" without
relying on the model to behave. The LLM/rule-based adapter never sees
the raw question without the resolved data attached.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from backend.copilot.llm_adapter import get_llm_adapter
from backend.copilot.tools import CopilotTools

KNOWN_SYMBOLS = ["NIFTY50", "NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "MIDCPNIFTY", "BANKEX"]


def _extract_symbol(question: str, default: str = "NIFTY50") -> str:
    q = question.upper()
    # Check longer/more specific names first — "NIFTY" is a substring of
    # "BANKNIFTY"/"FINNIFTY", so checking it first would misfire.
    for sym in sorted(KNOWN_SYMBOLS, key=len, reverse=True):
        if sym in q:
            return "NIFTY50" if sym == "NIFTY" else sym
    return default


def _get_candles(tools: CopilotTools, symbol: str, candles_by_symbol: Dict[str, List[Dict]]) -> List[Dict[str, Any]]:
    """Prefer caller-supplied candles; otherwise fetch live via the real
    broker client. Never fabricates — returns [] if neither has data."""
    if candles_by_symbol.get(symbol):
        return candles_by_symbol[symbol]
    live = tools.get_live_candles(symbol)
    return live["candles"] if live.get("available") else []


# Each entry: (keywords that must ALL appear, tool-call plan builder)
def _plan_market_status(tools: CopilotTools, q: str, candles_by_symbol: Dict[str, List[Dict]]) -> Dict[str, Any]:
    symbol = _extract_symbol(q)
    candles = _get_candles(tools, symbol, candles_by_symbol)
    ctx = {"market_status": tools.get_market_status()}
    if candles:
        ctx["indicators"] = tools.get_indicators(symbol, candles)
    return ctx


def _plan_direction(tools: CopilotTools, q: str, candles_by_symbol: Dict[str, List[Dict]]) -> Dict[str, Any]:
    symbol = _extract_symbol(q)
    candles = _get_candles(tools, symbol, candles_by_symbol)
    from backend.copilot.decision_engine import build_market_analysis
    if not candles:
        return {"analysis": {"available": False, "reason": f"No candle data available for {symbol} right now."}}
    return {"analysis": build_market_analysis(tools, symbol, candles).to_dict()}


def _plan_trade_opportunity(tools: CopilotTools, q: str, candles_by_symbol: Dict[str, List[Dict]]) -> Dict[str, Any]:
    symbol = _extract_symbol(q)
    return {"trade_plan": tools.get_trade_plan(symbol)}


def _plan_premium(tools: CopilotTools, q: str, candles_by_symbol: Dict[str, List[Dict]]) -> Dict[str, Any]:
    symbol = _extract_symbol(q)
    return {"trade_plan": tools.get_trade_plan(symbol)}


def _plan_risk(tools: CopilotTools, q: str, candles_by_symbol: Dict[str, List[Dict]]) -> Dict[str, Any]:
    return {"account_risk": tools.get_account_risk(), "open_positions": tools.get_open_positions()}


def _plan_positions(tools: CopilotTools, q: str, candles_by_symbol: Dict[str, List[Dict]]) -> Dict[str, Any]:
    return {"open_positions": tools.get_open_positions(), "account_risk": tools.get_account_risk()}


def _plan_trades_today(tools: CopilotTools, q: str, candles_by_symbol: Dict[str, List[Dict]]) -> Dict[str, Any]:
    return {"daily_pnl": tools.get_daily_pnl(), "recent_trades": tools.get_recent_trades(limit=20)}


def _plan_diagnostics(tools: CopilotTools, q: str, candles_by_symbol: Dict[str, List[Dict]]) -> Dict[str, Any]:
    return {"diagnostics": tools.run_full_diagnostics()}


def _plan_errors(tools: CopilotTools, q: str, candles_by_symbol: Dict[str, List[Dict]]) -> Dict[str, Any]:
    return {"recent_errors": tools.get_recent_errors(limit=20)}


def _plan_health(tools: CopilotTools, q: str, candles_by_symbol: Dict[str, List[Dict]]) -> Dict[str, Any]:
    return {"bot_health": tools.get_bot_health(), "market_status": tools.get_market_status(),
             "recent_errors": tools.get_recent_errors(limit=10)}


_INTENTS = [
    (["diagnos"], _plan_diagnostics),
    (["something is wrong", "check the bot", "check the complete bot"], _plan_diagnostics),
    (["today's errors", "todays errors", "check today's errors", "recent errors"], _plan_errors),
    (["premium not updating", "live premium", "not updating", "stale"], _plan_health),
    (["how much am i risking", "how much risk", "risking"], _plan_risk),
    (["current tradeplan", "current trade plan", "give me the current"], _plan_trade_opportunity),
    (["premium", "what is the current"], _plan_premium),
    (["open position", "continue holding", "should we hold", "should we continue"], _plan_positions),
    (["today's trades", "todays trades", "trades today", "show me today"], _plan_trades_today),
    (["why did we take", "why did it take", "why did the bot take", "why did it skip", "why did we skip"], _plan_trades_today),
    (["which side", "ce or pe", "which strike"], _plan_trade_opportunity),
    (["trade opportunity", "any trade", "which ce", "which pe", "watch"], _plan_trade_opportunity),
    (["bullish", "bearish", "direction", "trend"], _plan_direction),
    (["why are we not trading", "why aren't we trading", "why not trading", "why are we waiting"], _plan_trade_opportunity),
    (["how is the market", "market status", "market update", "how is nifty", "how is banknifty"], _plan_market_status),
]


def route_question(question: str) -> Any:
    q = question.lower()
    for keywords, plan_fn in _INTENTS:
        if any(kw in q for kw in keywords):
            return plan_fn
    return _plan_market_status  # sensible default rather than refusing


def chat(
    question: str,
    tools: CopilotTools,
    candles_by_symbol: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """The single entry point: resolves tools deterministically, then asks
    the configured adapter (local LLM or rule-based fallback) to explain
    the resolved data. Returns both the prose answer and the raw
    resolved context, so a caller (API/frontend) can show either."""
    candles_by_symbol = candles_by_symbol or {}
    plan_fn = route_question(question)
    context = plan_fn(tools, question, candles_by_symbol)

    adapter = get_llm_adapter()
    answer = adapter.explain(question, context)

    return {"question": question, "answer": answer, "resolved_context": context, "adapter": type(adapter).__name__}
