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

    def explain(self, question: str, context: Dict[str, Any]) -> str:
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
        if not ms.get("available"):
            return f"I couldn't check the market right now: {ms.get('reason', 'unknown reason')}"
        lines = ["MARKET STATUS"]
        lines.append(f"Market open: {ms.get('market_open')}")
        lines.append(f"WebSocket connected: {ms.get('websocket_connected')}, feed status: {ms.get('feed_status') or 'unknown'}")
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
