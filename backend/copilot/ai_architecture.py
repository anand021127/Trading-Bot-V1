"""Deterministic answer to "is there another AI making trading decisions?".

Why this is NOT left to the language model: a small local model told the
operator "there is no other AI in your bot — you are the sole AI
decision-maker", which is false. Questions about the AI ARCHITECTURE are
answered from live, authoritative state (the `ai` and `pipeline` context
sections) with fixed wording, exactly like "which symbol are you analyzing?".

Read-only. No tool calls, no provider calls, no secrets.
"""
from __future__ import annotations

from typing import Any, Dict

# Keywords that route a question here (checked BEFORE the generic buckets).
AI_ARCHITECTURE_KEYWORDS = (
    "another ai", "other ai", "second ai", "sole ai", "only ai", "two ai",
    "ai for decision", "ai decision making", "ai decision-making", "decision making ai",
    "ai making the decision", "ai making decision", "ai make decision", "ai makes decision",
    "ai trading decision", "who decides", "who makes the trading", "who is making the trading",
    "who takes the trad", "is ai making", "is the ai making", "does the bot use ai",
    "bot use ai", "is ai enabled", "is the ai enabled", "ai enabled", "ai layer", "ai engine",
    "is ai used", "is the ai used", "does ai trade", "does the ai trade",
)


def is_ai_architecture_question(question: str) -> bool:
    q = (question or "").lower()
    return any(k in q for k in AI_ARCHITECTURE_KEYWORDS)


def format_ai_architecture(context: Dict[str, Any]) -> str:
    ai: Dict[str, Any] = context.get("ai") or {}
    pipe: Dict[str, Any] = context.get("pipeline") or {}
    model = f"{ai.get('provider') or '?'} / {ai.get('model') or '?'}"

    if ai.get("available") is False:
        state = ("I can't read the AI Trading Decision engine's state right now "
                 f"({ai.get('reason') or 'state unavailable'}), so I can't confirm whether it is enabled.")
    elif ai.get("enabled"):
        state = (f"It is ENABLED (model {model}). Every V8-D BUY signal is sent to it before the hard "
                 "risk checks: APPROVE lets the trade continue to risk, sizing and execution; REJECT, "
                 "WAIT, a timeout or any failure means NO TRADE.")
    else:
        state = ("It is currently DISABLED, so no AI decision is being consulted: paper trading "
                 "runs on the V8-D strategy plus the risk controls only.")

    latest = (ai.get("latest") or {})
    if ai.get("enabled") and latest.get("available"):
        conf = latest.get("confidence")
        state += (f" Its latest decision was {latest.get('decision')}"
                  f"{f' (confidence {conf})' if conf is not None else ''}"
                  f"{', reasons: ' + ', '.join(latest.get('reason_codes') or []) if latest.get('reason_codes') else ''}.")

    live = ""
    if pipe:
        live = (f" On the latest scan: signal = {pipe.get('latest_signal')}, AI decision = "
                f"{pipe.get('ai_decision')}, risk = {pipe.get('risk_check')}, execution = {pipe.get('execution')}.")

    return (
        "Yes — there are two separate AI components in this bot, and I am not the one making trades.\n\n"
        "1) AI Trading Decision engine — a gate inside the paper trade pipeline. " + state +
        " It can only approve or block a V8-D signal; it can never change the price, stop, size or "
        "limits, and it can never bypass the kill switch, position sizing, daily trade limits or other "
        "risk controls." + live + "\n\n"
        "2) Copilot (me) — a read-only assistant that explains the bot's state, rejections, trades and "
        "backtests. I cannot place, approve, reject or modify any order or setting."
    )
