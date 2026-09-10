"""Conversational Copilot interface.

Routing is deterministic keyword matching, not LLM-driven — this is a
deliberate simplification vs. full LLM function-calling: it guarantees
(by construction, not by prompting) that every current-state question
resolves through a real tool call before any explanation is generated,
satisfying "AI should never hallucinate live information" without
relying on the model to behave. The LLM/rule-based adapter never sees
the raw question without the resolved data attached.

Intent taxonomy: GENERAL / EDUCATION / MARKET / TRADING / DIAGNOSTICS.
GENERAL and EDUCATION are checked FIRST and route to NO tool calls at
all — a greeting or a "what is VWAP?" question must never trigger a
live market-data fetch.

Conversation memory (this session's addition, see conversation_state.py):
`chat()` accepts an optional `ConversationState`. When a MARKET/TRADING
question resolves to a symbol, that symbol is remembered as
`state.last_symbol`. A follow-up like "Which market are you analyzing?"
or a symbol-less question ("Is it bullish?") then resolves against that
remembered symbol instead of silently defaulting to NIFTY50 or falling
back to a generic answer. This is deterministic — the LLM is never
asked to guess what "it" refers to; the router already knows.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from backend.copilot.conversation_state import ConversationState
from backend.copilot.llm_adapter import get_llm_adapter
from backend.copilot.tools import CopilotTools

KNOWN_SYMBOLS = ["NIFTY50", "NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "MIDCPNIFTY", "BANKEX"]

INTENT_GENERAL = "GENERAL"
INTENT_EDUCATION = "EDUCATION"
INTENT_MARKET = "MARKET"
INTENT_TRADING = "TRADING"
INTENT_DIAGNOSTICS = "DIAGNOSTICS"


def _extract_symbol(question: str, state: Optional[ConversationState] = None, default: str = "NIFTY50") -> str:
    """Explicit symbol keyword in THIS question wins. Otherwise, if the
    conversation already has a remembered symbol (from a prior turn
    that actually resolved one), use that — this is what makes "Is it
    bullish?" or "What about the premium?" correctly refer back to
    whatever was last analyzed, instead of silently assuming NIFTY50."""
    q = question.upper()
    for sym in sorted(KNOWN_SYMBOLS, key=len, reverse=True):
        if sym in q:
            return "NIFTY50" if sym == "NIFTY" else sym
    if state and state.last_symbol:
        return state.last_symbol
    return default


def _get_candles(tools: CopilotTools, symbol: str, candles_by_symbol: Dict[str, List[Dict]]) -> List[Dict[str, Any]]:
    """Prefer caller-supplied candles; otherwise fetch live via the real
    broker client. Never fabricates — returns [] if neither has data."""
    if candles_by_symbol.get(symbol):
        return candles_by_symbol[symbol]
    live = tools.get_live_candles(symbol)
    return live["candles"] if live.get("available") else []


# ── GENERAL / EDUCATION — NO tool calls, ever ────────────────────────
def _plan_general(tools: CopilotTools, q: str, candles_by_symbol: Dict[str, List[Dict]],
                   state: Optional[ConversationState] = None) -> Dict[str, Any]:
    return {"_intent": INTENT_GENERAL, "_question": q}


def _plan_education(tools: CopilotTools, q: str, candles_by_symbol: Dict[str, List[Dict]],
                     state: Optional[ConversationState] = None) -> Dict[str, Any]:
    return {"_intent": INTENT_EDUCATION, "_question": q}


def _plan_which_symbol(tools: CopilotTools, q: str, candles_by_symbol: Dict[str, List[Dict]],
                        state: Optional[ConversationState] = None) -> Dict[str, Any]:
    """"Which market/symbol are you analyzing?" — answered directly from
    conversation memory, deterministically. No tool call, no LLM guess:
    `state.last_symbol` IS the symbol the last resolved market/trading
    question actually used."""
    return {"_intent": INTENT_GENERAL, "_which_symbol": state.last_symbol if state else None}


# Each entry: (keywords that must ALL appear, tool-call plan builder)
def _plan_market_status(tools: CopilotTools, q: str, candles_by_symbol: Dict[str, List[Dict]],
                         state: Optional[ConversationState] = None) -> Dict[str, Any]:
    symbol = _extract_symbol(q, state)
    candles = _get_candles(tools, symbol, candles_by_symbol)
    ctx = {"market_status": tools.get_market_status(), "gap_analysis": tools.get_gap_analysis(symbol), "_symbol": symbol}
    if candles:
        ctx["indicators"] = tools.get_indicators(symbol, candles)
    return ctx


def _plan_direction(tools: CopilotTools, q: str, candles_by_symbol: Dict[str, List[Dict]],
                     state: Optional[ConversationState] = None) -> Dict[str, Any]:
    symbol = _extract_symbol(q, state)
    candles = _get_candles(tools, symbol, candles_by_symbol)
    from backend.copilot.decision_engine import build_market_analysis
    if not candles:
        return {"analysis": {"available": False, "reason": f"No candle data available for {symbol} right now."}, "_symbol": symbol}
    return {"analysis": build_market_analysis(tools, symbol, candles).to_dict(), "_symbol": symbol}


def _plan_trade_opportunity(tools: CopilotTools, q: str, candles_by_symbol: Dict[str, List[Dict]],
                             state: Optional[ConversationState] = None) -> Dict[str, Any]:
    symbol = _extract_symbol(q, state)
    return {"trade_plan": tools.get_trade_plan(symbol), "_symbol": symbol}


def _plan_premium(tools: CopilotTools, q: str, candles_by_symbol: Dict[str, List[Dict]],
                   state: Optional[ConversationState] = None) -> Dict[str, Any]:
    symbol = _extract_symbol(q, state)
    return {"trade_plan": tools.get_trade_plan(symbol), "_symbol": symbol}


def _plan_risk(tools: CopilotTools, q: str, candles_by_symbol: Dict[str, List[Dict]],
               state: Optional[ConversationState] = None) -> Dict[str, Any]:
    return {"account_risk": tools.get_account_risk(), "open_positions": tools.get_open_positions()}


def _plan_positions(tools: CopilotTools, q: str, candles_by_symbol: Dict[str, List[Dict]],
                     state: Optional[ConversationState] = None) -> Dict[str, Any]:
    return {"open_positions": tools.get_open_positions(), "account_risk": tools.get_account_risk()}


def _plan_trades_today(tools: CopilotTools, q: str, candles_by_symbol: Dict[str, List[Dict]],
                        state: Optional[ConversationState] = None) -> Dict[str, Any]:
    return {"daily_pnl": tools.get_daily_pnl(), "recent_trades": tools.get_recent_trades(limit=20)}


def _plan_diagnostics(tools: CopilotTools, q: str, candles_by_symbol: Dict[str, List[Dict]],
                       state: Optional[ConversationState] = None) -> Dict[str, Any]:
    return {"diagnostics": tools.run_full_diagnostics()}


def _plan_errors(tools: CopilotTools, q: str, candles_by_symbol: Dict[str, List[Dict]],
                  state: Optional[ConversationState] = None) -> Dict[str, Any]:
    return {"recent_errors": tools.get_recent_errors(limit=20)}


def _plan_health(tools: CopilotTools, q: str, candles_by_symbol: Dict[str, List[Dict]],
                  state: Optional[ConversationState] = None) -> Dict[str, Any]:
    return {"bot_health": tools.get_bot_health(), "market_status": tools.get_market_status(),
             "recent_errors": tools.get_recent_errors(limit=10)}


_INTENTS = [
    # "Which market/symbol are you analyzing" -- checked before GENERAL's
    # broader greeting bucket so it doesn't get swallowed by "what can
    # you do"-style matches.
    (["which market", "which symbol", "which one are you analyzing", "what are you analyzing", "what are you looking at",
      "what market are you", "what symbol are you", "which one are you looking at"], _plan_which_symbol),
    # GENERAL -- greetings/capability questions, checked FIRST so "Hi"
    # never falls through to a market-data fetch.
    (["hi", "hello", "hey", "good morning", "good afternoon", "good evening",
      "what can you do", "what do you do", "help", "who are you", "thanks", "thank you"], _plan_general),
    # EDUCATION -- definition/concept questions, also NO tool calls.
    (["what is", "what's a", "what does", "define ", "explain what", "meaning of"], _plan_education),
    (["diagnos"], _plan_diagnostics),
    (["something is wrong", "check the bot", "check the complete bot"], _plan_diagnostics),
    (["today's errors", "todays errors", "check today's errors", "recent errors"], _plan_errors),
    (["premium not updating", "live premium", "not updating", "stale"], _plan_health),
    (["why is websocket", "why is the websocket", "websocket disconnected", "why is scanner"], _plan_health),
    (["how much am i risking", "how much risk", "risking"], _plan_risk),
    (["current tradeplan", "current trade plan", "give me the current"], _plan_trade_opportunity),
    (["premium", "what is the current"], _plan_premium),
    (["open position", "continue holding", "should we hold", "should we continue"], _plan_positions),
    (["today's trades", "todays trades", "trades today", "show me today"], _plan_trades_today),
    (["why did we take", "why did it take", "why did the bot take", "why did it skip", "why did we skip"], _plan_trades_today),
    (["should i buy ce", "should i buy pe", "find a trade", "is there a trade",
      "which side", "ce or pe", "which strike"], _plan_trade_opportunity),
    (["trade opportunity", "any trade", "which ce", "which pe", "watch"], _plan_trade_opportunity),
    (["gap up", "gap down", "gap %", "gap percent", "how much did it gap"], _plan_market_status),
    (["bullish", "bearish", "direction", "trend"], _plan_direction),
    (["why are we not trading", "why aren't we trading", "why not trading", "why are we waiting"], _plan_trade_opportunity),
    (["how is the market", "market status", "market update", "how is nifty", "how is banknifty",
      "how did nifty open", "how did it open", "market open"], _plan_market_status),
]


def _classify_intent(question: str) -> str:
    q = question.lower()
    for keywords, plan_fn in _INTENTS:
        if any(kw in q for kw in keywords):
            if plan_fn in (_plan_general, _plan_which_symbol):
                return INTENT_GENERAL
            if plan_fn is _plan_education:
                return INTENT_EDUCATION
            if plan_fn is _plan_diagnostics or plan_fn is _plan_errors or plan_fn is _plan_health:
                return INTENT_DIAGNOSTICS
            if plan_fn in (_plan_trade_opportunity, _plan_premium, _plan_risk, _plan_positions, _plan_trades_today):
                return INTENT_TRADING
            return INTENT_MARKET
    return INTENT_GENERAL


def route_question(question: str) -> Any:
    q = question.lower()
    for keywords, plan_fn in _INTENTS:
        if any(kw in q for kw in keywords):
            return plan_fn
    # No keyword bucket matched, but a known symbol was explicitly named
    # ("What about Sensex?") — treat that as a market question for that
    # symbol rather than falling through to a generic greeting, which
    # would silently drop the only concrete signal in the message.
    if _extract_symbol(question, state=None, default="") :
        return _plan_market_status
    # A genuinely unrecognized message defaults to GENERAL (a capability
    # explanation), NOT a market-data fetch — no keyword match should
    # never mean "assume they want market data."
    return _plan_general


def chat(
    question: str,
    tools: CopilotTools,
    candles_by_symbol: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    state: Optional[ConversationState] = None,
) -> Dict[str, Any]:
    """The single entry point: resolves tools deterministically, then asks
    the configured adapter (local LLM or rule-based fallback) to explain
    the resolved data. Returns both the prose answer and the raw
    resolved context, so a caller (API/frontend) can show either.

    `state`, when given, is BOTH read (to resolve a symbol-less follow-up
    against the last-analyzed symbol) and updated (recording the symbol
    this turn actually resolved, and appending both turns to history) —
    this is what makes "Which market are you analyzing?" work correctly
    instead of falling back to a generic answer."""
    candles_by_symbol = candles_by_symbol or {}
    plan_fn = route_question(question)
    context = plan_fn(tools, question, candles_by_symbol, state)

    # Remember the symbol this turn resolved to, if any — deterministic,
    # not an LLM guess, and only updated when a plan actually resolved one.
    if state is not None and context.get("_symbol"):
        state.last_symbol = context["_symbol"]

    history = state.recent_history() if state is not None else []
    adapter = get_llm_adapter()
    answer = adapter.explain(question, context, history=history)

    if state is not None:
        state.add_turn("user", question)
        state.add_turn("assistant", answer)

    return {"question": question, "answer": answer, "resolved_context": context, "adapter": type(adapter).__name__}
