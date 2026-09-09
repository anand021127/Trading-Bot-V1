"""Pluggable reasoning/explanation backend for the Copilot.

Two backends:
  - RuleBasedFallbackAdapter: template-based, zero dependencies, zero
    cost, always available. This is the DEFAULT (COPILOT_LLM_BACKEND=none)
    so the Copilot works out of the box with no model download at all.
  - LocalOpenAICompatibleAdapter: talks to a local OpenAI-chat-compatible
    server (Ollama `ollama serve`, llama.cpp's `server`, vLLM, etc.) over
    HTTP on localhost — genuinely zero API cost, runs on the operator's
    own machine, no Anthropic/OpenAI key required. If that server isn't
    reachable, it falls back to the rule-based adapter rather than
    failing the whole request.

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
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Dict

from backend.copilot.config import CopilotSettings, load_copilot_settings


class LLMAdapter(ABC):
    @abstractmethod
    def explain(self, question: str, context: Dict[str, Any]) -> str:
        """Turn already-resolved tool output (`context`) into a natural-
        language answer to `question`. Must never introduce a number,
        price, or state that isn't present in `context`."""
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

    def explain(self, question: str, context: Dict[str, Any]) -> str:
        intent = context.get("_intent")
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


class LocalOpenAICompatibleAdapter(LLMAdapter):
    """Talks to a local OpenAI-chat-compatible HTTP server. Falls back to
    the rule-based adapter on any connection/timeout/parse error — a
    local model being offline must never break the Copilot."""

    def __init__(self, settings: CopilotSettings) -> None:
        self.settings = settings
        self._fallback = RuleBasedFallbackAdapter()

    def explain(self, question: str, context: Dict[str, Any]) -> str:
        intent = context.get("_intent")
        if intent in ("GENERAL", "EDUCATION"):
            # Conversational/educational — no live data involved, so the
            # LLM can just answer naturally. Still deterministic-safe: if
            # it's unreachable, falls back to the same canned responses.
            system_prompt = (
                "You are a friendly trading-bot assistant. Answer briefly and naturally. "
                "You are NEVER given live market data for this kind of question, so do not "
                "invent any price, indicator, or trade detail — if asked about the market, "
                "say you'd need to check current data for that."
            )
            user_prompt = question
        else:
            system_prompt = (
                "You are a trading bot's explanation assistant. You are given "
                "ALREADY-COMPUTED structured data below. Explain it in plain "
                "language, in at most 6 sentences. Do NOT invent any price, "
                "percentage, or status that is not present in the data. If the "
                "data says something is unavailable, say so plainly instead of "
                "guessing."
            )
            user_prompt = f"Question: {question}\n\nData:\n{json.dumps(context, indent=2, default=str)}"

        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 400,
        }
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.settings.llm_timeout_seconds) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError, OSError) as e:
            fallback_text = self._fallback.explain(question, context)
            return f"[local LLM unavailable ({e}) — showing raw verified data instead]\n{fallback_text}"


def get_llm_adapter(settings: CopilotSettings = None) -> LLMAdapter:
    settings = settings or load_copilot_settings()
    if settings.llm_backend == "local_openai_compatible":
        return LocalOpenAICompatibleAdapter(settings)
    return RuleBasedFallbackAdapter()
