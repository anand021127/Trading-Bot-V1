"""Pluggable reasoning/explanation backend for the Copilot.

Two backends:
  - RuleBasedFallbackAdapter: template-based, zero dependencies, zero
    cost, always available. This is the DEFAULT (COPILOT_LLM_BACKEND=none)
    so the Copilot works out of the box with no model download at all.
  - LocalOpenAICompatibleAdapter: talks to a local OpenAI-chat-compatible
    server (Ollama `ollama serve`, llama.cpp's `server`, vLLM, etc.) over
    HTTP on localhost — genuinely zero API cost, runs on the operator's
    own machine, no API key required.
  - RemoteOpenAICompatibleAdapter: talks to an OpenAI-compatible cloud
    endpoint using a key from the environment (never hard-coded, never
    logged, never sent anywhere but the provider).

FAILURE CONTRACT (changed deliberately): a provider being unreachable,
timing out, rejecting credentials, rate-limiting, or missing the model
now RAISES a typed AIProviderError (see provider_errors.py). It is NO
LONGER silently converted into a rule-based canned answer — an operator
asking a live-data question must never be fooled into thinking a
template answer came from their model. The rule-based adapter remains
available explicitly (backend "none"), and the deterministic
"which market are you analyzing?" memory answer still never touches
the network.

Neither adapter is ever given write access to anything — `explain()`
takes already-computed structured data and returns a string. It cannot
call tools itself in this implementation (see conversational.py, which
resolves ALL tool calls deterministically before the adapter ever runs),
which is a deliberate, simpler alternative to full LLM function-calling:
it makes "the LLM can't skip the tool and hallucinate a number" true by
construction, not just by prompting.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from backend.copilot.config import CopilotSettings, load_copilot_settings
from backend.copilot.conversation_state import ConversationTurn
from backend.copilot.provider_errors import (
    AIProviderAuthError,
    AIProviderError,
    classify_provider_exception,
)


class LLMAdapter(ABC):
    @abstractmethod
    def explain(self, question: str, context: Dict[str, Any], history: Optional[List[ConversationTurn]] = None) -> str:
        """Turn already-resolved tool output (`context`) into a natural-
        language answer to `question`. Must never introduce a number,
        price, or state that isn't present in `context`. `history`, when
        given, is recent (role, text) conversation turns — used for
        continuity on GENERAL/EDUCATION follow-ups; it is NOT a source of
        live data and must not be treated as one."""
        raise NotImplementedError


class RuleBasedFallbackAdapter(LLMAdapter):
    """No model, no network call, no cost. Templates KNOWN context shapes
    (trade_plan, analysis, diagnostics, positions, daily_pnl) into
    human-readable prose matching the format an operator actually wants
    to read — not a JSON dump. Falls back to a generic key listing only
    for shapes it doesn't recognize. Never introduces a number that
    isn't already in `context` — every value printed here is read
    straight out of the resolved tool output, not computed or guessed."""

    # Small, deterministic knowledge base for EDUCATION questions — zero
    # cost, no model needed. Matched by substring on the question, longest
    # term first so e.g. "gap up" doesn't get shadowed by a shorter "gap".
    EDUCATION_TERMS: Dict[str, str] = {
        "gap up": "A gap up is when a stock/index opens today's session above yesterday's closing price, "
                  "with no trading in between — often driven by overnight news.",
        "gap down": "A gap down is when a stock/index opens today's session below yesterday's closing price, "
                    "with no trading in between.",
        "vwap": "VWAP (Volume-Weighted Average Price) is the average price a security has traded at "
                "today, weighted by volume. Price above VWAP is often read as intraday bullish pressure, "
                "below as bearish.",
        "ema": "EMA (Exponential Moving Average) is a moving average that weights recent prices more "
               "heavily than older ones, so it reacts faster to new price action than a simple average.",
        "rsi": "RSI (Relative Strength Index) measures recent price momentum on a 0-100 scale. "
               "Above ~70 is often read as overbought, below ~30 as oversold.",
        "atr": "ATR (Average True Range) measures how much a price typically moves over a given period — "
               "a volatility measure, not a direction indicator.",
        "choppiness index": "The Choppiness Index measures whether a market is trending or ranging, "
                             "on a 0-100 scale — high values suggest a choppy/ranging market, low values a trending one.",
        "support": "Support is a price level where buying pressure has historically been strong enough "
                   "to stop a decline.",
        "resistance": "Resistance is a price level where selling pressure has historically been strong "
                      "enough to stop a rally.",
        "ce": "CE (Call European/Call option) gives the buyer the right to buy the underlying at a fixed "
              "strike price — bought when expecting the price to rise.",
        "pe": "PE (Put European/Put option) gives the buyer the right to sell the underlying at a fixed "
              "strike price — bought when expecting the price to fall.",
        "atm": "ATM (At-The-Money) describes an option contract whose strike price is closest to the "
               "current underlying price.",
        "oi": "OI (Open Interest) is the total number of outstanding option/futures contracts that "
              "haven't been closed — higher OI generally means more liquidity.",
        "iv": "IV (Implied Volatility) is the market's expectation of how much an underlying will move, "
              "derived from option prices — higher IV means pricier options.",
        "delta": "Delta measures how much an option's price is expected to move for a ₹1 move in the "
                 "underlying — roughly the option's directional exposure.",
        "theta": "Theta measures how much an option's price is expected to decay per day, all else equal "
                 "— option buyers lose value to theta as expiry approaches.",
        "stop loss": "A stop loss is a predefined price at which a losing trade is exited to cap risk.",
        "risk reward": "Risk/reward is the ratio between what you stand to lose (risk, to the stop loss) "
                       "and what you stand to gain (reward, to the target) on a trade.",
        "lot size": "Lot size is the fixed number of underlying units one options contract represents — "
                    "you can only trade in whole multiples of it.",
    }

    def explain(self, question: str, context: Dict[str, Any], history: Optional[List[ConversationTurn]] = None) -> str:
        intent = context.get("_intent")
        if "_which_symbol" in context:
            return self._format_which_symbol(context["_which_symbol"])
        if intent == "GENERAL":
            return self._format_general(context.get("_question", question))
        if intent == "EDUCATION":
            return self._format_education(context.get("_question", question))

        # Prefer the most specific, richest shape available.
        if "trade_plan" in context:
            return self._format_trade_plan_result(context["trade_plan"])
        if "diagnostics" in context:
            return self._format_diagnostics(context["diagnostics"])
        if "analysis" in context and "trade_plan" not in context:
            return self._format_analysis_only(context["analysis"])
        if "market_status" in context:
            return self._format_market_status(context)
        if "open_positions" in context:
            return self._format_positions(context)
        if "daily_pnl" in context:
            return self._format_daily_pnl(context)
        if "bot_health" in context:
            return self._format_health(context)
        return self._generic(context)

    def _format_which_symbol(self, last_symbol: Optional[str]) -> str:
        if not last_symbol:
            return ("I haven't analyzed a specific market yet in this conversation — ask me about "
                    "NIFTY50, BANKNIFTY, SENSEX, or another supported symbol.")
        return last_symbol

    def _format_general(self, question: str) -> str:
        q = question.lower()
        if any(g in q for g in ("thanks", "thank you")):
            return "You're welcome! Let me know if you want a market update or a trade check."
        if any(g in q for g in ("hi", "hello", "hey", "good morning", "good afternoon", "good evening")):
            return ("Hi! I'm your trading Copilot. I can check the market, explain a term, look for a "
                    "trade opportunity, or run bot diagnostics — just ask.")
        return (
            "I can help with a few things:\n"
            "- Market: \"How is the market?\", \"Is NIFTY bullish?\", \"Did it gap up?\"\n"
            "- Trading: \"Any trade opportunity?\", \"Should I buy CE?\", \"Why did we skip?\"\n"
            "- Positions: \"Check my open position\", \"How much am I risking?\"\n"
            "- Diagnostics: \"Check the complete bot\", \"Why is the WebSocket down?\"\n"
            "- Education: \"What is VWAP?\", \"What is a gap up?\"\n\n"
            "Everything is PAPER mode only — no live orders are ever placed."
        )

    def _format_education(self, question: str) -> str:
        q = question.lower()
        for term in sorted(self.EDUCATION_TERMS, key=len, reverse=True):
            if term in q:
                return self.EDUCATION_TERMS[term]
        return ("I don't have a canned definition for that one yet. I can explain VWAP, EMA, RSI, ATR, "
                "gap up/down, support/resistance, CE/PE, ATM, OI, IV, delta, theta, stop loss, risk/reward, "
                "or lot size — try asking about one of those.")

    # ── Known-shape formatters ────────────────────────────────────────
    def _format_trade_plan_result(self, result: Dict[str, Any]) -> str:
        if not isinstance(result, dict) or result.get("available") is False:
            reason = result.get("reason", "unknown reason") if isinstance(result, dict) else "unknown reason"
            return f"I couldn't check that right now: {reason}"

        analysis = result.get("analysis") or {}
        decision = result.get("decision")
        plan = result.get("trade_plan")
        validation = result.get("validation")
        reason = result.get("reason", "")

        header_bits = []
        if analysis.get("direction"):
            header_bits.append(f"Direction: {analysis['direction']}")
        if analysis.get("confidence") is not None:
            header_bits.append(f"Confidence: {analysis['confidence']}%")
        if analysis.get("market_regime"):
            header_bits.append(f"Regime: {analysis['market_regime']}")

        if analysis.get("data_status") == "STALE":
            lines = ["No trade right now.", "",
                     "The latest market candle is not considered fresh enough for a safe "
                     "trade decision, so trading is blocked until fresh data is available."]
            if analysis.get("data_age_seconds") is not None:
                lines.append(f"(Data age: {analysis['data_age_seconds']:.0f}s, as of {analysis.get('candle_timestamp', 'unknown')}.)")
            return "\n".join(lines)

        if decision == "TRADE" and plan:
            lines = ["Paper trade opportunity detected:", ""]
            side = plan.get("option_type", "")
            lines.append(f"Direction: BUY {side}" if side else "Direction: BUY")
            if plan.get("strike") is not None:
                lines.append(f"Strike: {plan['strike']}")
            if plan.get("expiry"):
                lines.append(f"Expiry: {plan['expiry']}")
            if plan.get("entry_price_low") is not None:
                lines.append(f"Entry: ₹{plan['entry_price_low']:.2f}–₹{plan['entry_price_high']:.2f}")
            if plan.get("stop_loss") is not None:
                lines.append(f"Stop Loss: ₹{plan['stop_loss']:.2f}")
            if plan.get("target_1") is not None:
                lines.append(f"Target: ₹{plan['target_1']:.2f}")
            if plan.get("risk_reward") is not None:
                lines.append(f"Risk/Reward: {plan['risk_reward']}")
            if analysis.get("confidence") is not None:
                lines.append(f"Confidence: {analysis['confidence']}%")
            lines.append("")
            lines.append(f"Risk checks: {'PASSED' if (validation or {}).get('approved') else 'PENDING'}")
            lines.append("Execution mode: PAPER")
            lines.append("")
            lines.append("No live order was sent.")
            return "\n".join(lines)

        # SKIP / WAIT / no setup — explain exactly why, not generically.
        lines = ["No trade right now."]
        if header_bits:
            lines.append("")
            lines.append(" · ".join(header_bits))
        specific_reason = reason or (validation or {}).get("reasons_rejected") or analysis.get("decision_reason")
        if isinstance(specific_reason, list):
            specific_reason = "; ".join(specific_reason)
        if specific_reason:
            lines.append("")
            lines.append(f"Reason: {specific_reason}")
        else:
            lines.append("")
            lines.append("The current strategy does not have a qualifying option setup right now.")
        return "\n".join(lines)

    def _format_analysis_only(self, analysis: Dict[str, Any]) -> str:
        if not analysis or analysis.get("available") is False:
            return f"I couldn't check the market right now: {analysis.get('reason', 'unknown reason') if analysis else 'no data'}"
        lines = ["MARKET STATUS"]
        if analysis.get("data_status"):
            lines.append(f"Data freshness: {analysis['data_status']}")
        if analysis.get("direction"):
            lines.append(f"Direction: {analysis['direction']}" + (f" ({analysis['confidence']}% confidence)" if analysis.get("confidence") is not None else ""))
        if analysis.get("market_regime"):
            lines.append(f"Regime: {analysis['market_regime']}")
        if analysis.get("support") is not None:
            lines.append(f"Support: {analysis['support']:.2f}")
        if analysis.get("resistance") is not None:
            lines.append(f"Resistance: {analysis['resistance']:.2f}")
        if analysis.get("momentum") is not None:
            lines.append(f"Momentum (RSI): {analysis['momentum']}")
        if analysis.get("preferred_side"):
            lines.append(f"Option opportunity status: watching {analysis['preferred_side']}")
        else:
            lines.append("Option opportunity status: none right now")
        return "\n".join(lines)

    def _format_market_status(self, context: Dict[str, Any]) -> str:
        ms = context.get("market_status", {})
        ind = context.get("indicators", {})
        gap = context.get("gap_analysis", {})
        if not ms.get("available"):
            return f"I couldn't check the market right now: {ms.get('reason', 'unknown reason')}"
        lines = ["MARKET STATUS"]
        market_open = ms.get("market_open")
        lines.append(f"Market open: {market_open}" if market_open is not None else "Market open: unknown")
        if market_open is False:
            lines.append("(Market is closed — this is expected outside session hours, not an error.)")
        lines.append(f"WebSocket connected: {ms.get('websocket_connected')}, feed status: {ms.get('feed_status') or 'unknown'}")
        if gap and gap.get("available"):
            lines.append("")
            lines.append(f"Gap: {gap['classification']} ({gap['gap_points']:+.2f} pts, {gap['gap_percent']:+.2f}%) "
                          f"— prev close {gap['previous_close']}, today open {gap['today_open']}")
        if ind and ind.get("available"):
            lines.append("")
            lines.append(f"NIFTY50 spot: {ind.get('last_close')}")
            if ind.get("rsi") is not None:
                lines.append(f"RSI: {ind['rsi']}")
            if ind.get("ema20") is not None and ind.get("ema50") is not None:
                lines.append(f"EMA20/EMA50: {ind['ema20']:.2f} / {ind['ema50']:.2f}")
            if ind.get("vwap") is not None:
                lines.append(f"VWAP: {ind['vwap']:.2f}")
            if ind.get("as_of"):
                lines.append(f"Data as of: {ind['as_of']}")
        elif ind and not ind.get("available"):
            lines.append("")
            lines.append(f"Indicators unavailable: {ind.get('reason', 'unknown reason')}")
        return "\n".join(lines)

    def _format_diagnostics(self, diag: Dict[str, Any]) -> str:
        if not diag.get("available"):
            return f"Couldn't run diagnostics: {diag.get('reason', 'unknown reason')}"
        lines = [f"Overall bot health: {diag.get('overall_status', 'UNKNOWN')}", ""]
        problems = [r for r in diag.get("rows", []) if r.get("status") not in ("OK",)]
        if not problems:
            lines.append("All checked components are OK.")
        else:
            for r in problems:
                lines.append(f"- {r['component']}: {r['status']} — {r.get('problem') or 'no detail'}")
        return "\n".join(lines)

    def _format_positions(self, context: Dict[str, Any]) -> str:
        pos = context.get("open_positions", {})
        if not pos.get("available"):
            return f"Couldn't check positions: {pos.get('reason', 'unknown reason')}"
        positions = pos.get("positions", [])
        if not positions:
            return "No open position right now."
        lines = [f"{len(positions)} open position(s):"]
        for p in positions:
            lines.append(f"- {p.get('symbol', '?')}: qty {p.get('quantity', '?')} @ ₹{p.get('average_price', '?')}")
        return "\n".join(lines)

    def _format_daily_pnl(self, context: Dict[str, Any]) -> str:
        pnl = context.get("daily_pnl", {})
        if not pnl.get("available"):
            return f"Couldn't check today's P&L: {pnl.get('reason', 'unknown reason')}"
        return f"Today ({pnl.get('date', '')}): {pnl.get('trades_today', 0)} trade(s), realized P&L ₹{pnl.get('realized_pnl', 0):.2f}"

    def _format_health(self, context: Dict[str, Any]) -> str:
        health = context.get("bot_health", {})
        if not health.get("available"):
            return f"Couldn't check bot health: {health.get('reason', 'unknown reason')}"
        return f"Bot health: {json.dumps(health, default=str)}"

    def _generic(self, context: Dict[str, Any]) -> str:
        lines = []
        for key, value in context.items():
            if isinstance(value, dict) and value.get("available") is False:
                lines.append(f"- {key}: unavailable ({value.get('reason', 'unknown reason')})")
            elif isinstance(value, (dict, list)):
                lines.append(f"- {key}: {json.dumps(value, default=str)}")
            else:
                lines.append(f"- {key}: {value}")
        body = "\n".join(lines) if lines else "No data was resolved for this question."
        return f"Here's what I could verify:\n{body}"


def _provider_chat(
    messages: List[Dict[str, str]],
    *,
    base_url: str,
    model: str,
    timeout_seconds: float,
    api_key: Optional[str] = None,
) -> str:
    """Single shared OpenAI-compatible chat-completions call.

    Raises a typed AIProviderError subclass for every failure mode —
    connection refused, timeout, 401/403, 404 model-missing, 429,
    malformed response — instead of returning a fabricated answer.
    The API key (when present) is used ONLY in the Authorization header
    to the provider endpoint.
    """
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 400,
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
        raise classify_provider_exception(e)
    except ValueError as e:  # json.JSONDecodeError and friends
        raise AIProviderError(f"Malformed response from AI provider: {e}")
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise AIProviderError(f"Malformed response from AI provider: {e}")


class LocalOpenAICompatibleAdapter(LLMAdapter):
    """Talks to a local OpenAI-chat-compatible HTTP server. Falls back to
    the rule-based adapter on any connection/timeout/parse error — a
    local model being offline must never break the Copilot."""

    def __init__(self, settings: CopilotSettings) -> None:
        self.settings = settings
        self._fallback = RuleBasedFallbackAdapter()

    def explain(self, question: str, context: Dict[str, Any], history: Optional[List[ConversationTurn]] = None) -> str:
        # "Which market are you analyzing?" is answered deterministically
        # regardless of backend — this is conversation MEMORY, not
        # something to hand to the model to (possibly wrongly) infer.
        if "_which_symbol" in context:
            return self._fallback._format_which_symbol(context["_which_symbol"])

        intent = context.get("_intent")
        history_messages = [{"role": t.role, "content": t.text} for t in (history or [])]

        base_system = (
            "You are the conversational interface for a trading bot's Copilot. "
            "Follow these rules strictly:\n"
            "1. Never invent market prices, indicators, option premiums, positions, "
            "quantities, or trade results — only state what is explicitly given to you.\n"
            "2. Use the structured data provided for any current-state question; you are "
            "not given tools to call yourself, so if data isn't in what you're given, "
            "say plainly that you don't have it.\n"
            "3. Clearly distinguish live/current data from historical or backtest data "
            "when the context indicates which one it is.\n"
            "4. If something is marked unavailable or stale in the data, say so — never "
            "paper over a gap with a guess.\n"
            "5. Never claim a trade was executed, a paper position was opened, or an "
            "order was placed unless the data explicitly confirms it — you have no "
            "ability to place, modify, or cancel any order yourself.\n"
            "6. You may use the recent conversation history for context (e.g. resolving "
            "\"it\"/\"that\" to whatever was discussed), but never invent new facts from it."
        )

        if intent in ("GENERAL", "EDUCATION"):
            # Conversational/educational — no live data involved, so the
            # LLM can just answer naturally. Still deterministic-safe: if
            # it's unreachable, falls back to the same canned responses.
            system_prompt = base_system + (
                "\n\nThis particular message is general conversation or an educational "
                "question — you were NOT given any live market data for it. If asked "
                "about current market state, say you'd need to check current data."
            )
            user_prompt = question
        else:
            system_prompt = base_system + (
                "\n\nBelow is ALREADY-COMPUTED structured data resolved by real tools for "
                "this question. Explain it in plain language, in at most 6 sentences."
            )
            user_prompt = f"Question: {question}\n\nData:\n{json.dumps(context, indent=2, default=str)}"

        messages = [{"role": "system", "content": system_prompt}] + history_messages + \
                   [{"role": "user", "content": user_prompt}]
        return self._send_chat(messages)

    def _send_chat(self, messages: List[Dict[str, str]]) -> str:
        """Send the built messages to the configured provider. Raises a
        typed AIProviderError subclass on ANY failure — provider problems
        are surfaced honestly, never swapped for a canned answer."""
        return _provider_chat(
            messages,
            base_url=self.settings.llm_base_url,
            model=self.settings.llm_model,
            timeout_seconds=self.settings.llm_timeout_seconds,
            api_key=None,
        )


class RemoteOpenAICompatibleAdapter(LocalOpenAICompatibleAdapter):
    """OpenAI-compatible cloud provider (default: api.openai.com/v1).

    Inherits the deterministic which-symbol path and the grounded
    message construction; only the transport differs — an API key from
    the environment is required and sent solely as the provider
    Authorization header. Missing key raises a typed auth error at ask
    time so the UI can show a clear configuration message instead of a
    fake answer.
    """

    DEFAULT_BASE_URL = "https://api.openai.com/v1"

    def _send_chat(self, messages: List[Dict[str, str]]) -> str:
        if not self.settings.ai_api_key:
            raise AIProviderAuthError(
                f"No API key configured for the remote AI provider. Set "
                f"{self.settings.ai_api_key_env} in the backend environment."
            )
        base_url = os.getenv("COPILOT_AI_BASE_URL", self.DEFAULT_BASE_URL).strip() or self.DEFAULT_BASE_URL
        return _provider_chat(
            messages,
            base_url=base_url,
            model=self.settings.llm_model,
            timeout_seconds=self.settings.llm_timeout_seconds,
            api_key=self.settings.ai_api_key,
        )


def get_llm_adapter(settings: CopilotSettings = None) -> LLMAdapter:
    settings = settings or load_copilot_settings()
    if settings.llm_backend in ("local_openai_compatible", "ollama"):
        return LocalOpenAICompatibleAdapter(settings)
    if settings.llm_backend == "openai":
        return RemoteOpenAICompatibleAdapter(settings)
    return RuleBasedFallbackAdapter()
