"""Deterministic answer to "why didn't the bot trade?" / "explain the latest rejection".

The facts are fully known from the persisted scan record (the SAME pipeline view
the dashboard shows), so they are never left to a language model — a slow or
failing Copilot model must not hide the real reason, and must never be confused
with the AI Trading Decision gate. Read-only; no tool/provider calls.
"""
from __future__ import annotations

from typing import Any, Dict, List

WHY_NO_TRADE_KEYWORDS = (
    "didn't trade", "didnt trade", "did not trade", "didn't the bot trade", "why no trade", "no trades",
    "not trading", "isn't trading", "isnt trading", "why isn't", "why wasn't", "why hasn't", "why has the bot not",
    "why did the bot not", "why the bot didn't", "why bot didn't", "why bot did not", "no signal", "no buy",
    "zero trades", "zero signal", "why is there no", "not taking trades", "not take any trade",
    "why no signal", "latest rejection", "last rejection", "why was it rejected", "why rejected",
)


def is_why_no_trade_question(question: str) -> bool:
    q = (question or "").lower().replace("’", "'")
    return any(k in q for k in WHY_NO_TRADE_KEYWORDS)


def _conds(v8d: Dict[str, Any], side: str) -> List[str]:
    s = (v8d or {}).get(side.lower()) or {}
    lines: List[str] = []
    for k in (s.get("failed") or []):
        c = s.get(k)
        detail = c.get("detail") if isinstance(c, dict) else None
        lines.append(f"  - {k}: {detail}" if detail else f"  - {k} did not pass")
    return lines


def format_why_no_trade(context: Dict[str, Any]) -> str:
    p: Dict[str, Any] = context.get("pipeline") or {}
    outcome = p.get("outcome")
    sym = p.get("primary_symbol") or "the scanned symbol"
    when = p.get("scan_time_ist") or "the latest scan"
    chain = (
        f"AI Trading Decision: {p.get('ai_decision')} · Risk: {p.get('risk_check')} · "
        f"Execution: {p.get('execution')} · Final: {p.get('final')}."
    ) if p else ""

    if not p or not outcome:
        return ("No scan has been recorded yet, so there is nothing to explain. "
                + (str(p.get("summary")) if p.get("summary") else "Start the bot and wait for the first scan."))

    head: str
    body: List[str] = []
    if outcome == "NO_SIGNAL":
        v8 = p.get("v8d") or {}
        head = (f"No trade — V8-D evaluated {sym} at {when} and found NO SIGNAL: the technical setup did not "
                "exist. This is not a rejection and the AI was not involved.")
        if v8.get("evaluated"):
            side = v8.get("closest_side") or "CE"
            body.append(f"Closest side: {side}. Failed condition(s):")
            body += _conds(v8, side) or [f"  - {', '.join(v8.get('failed') or [])}"]
            px = v8.get("price") or {}
            body.append(f"Values: EMA20 {v8.get('ema20'):.2f} · EMA50 {v8.get('ema50'):.2f} · "
                        f"separation {v8.get('ema_separation_pct'):+.3f}% · RSI {v8.get('rsi'):.2f} · "
                        f"close {px.get('close')}" if v8.get("ema20") is not None else "")
        elif p.get("signal_detail"):
            body.append(str(p["signal_detail"]))
    elif outcome == "SIGNAL_REJECTED":
        head = (f"No trade — V8-D found a technical setup on {sym} at {when} but REJECTED the signal: "
                f"{p.get('signal_detail') or 'see rejection reasons'}.")
    elif outcome == "AI_REJECTED":
        head = (f"No trade — V8-D produced {p.get('latest_signal')} on {sym} at {when}, but the AI Trading "
                f"Decision gate stopped it: {p.get('ai_decision')}"
                f"{' (' + p['ai_reason'] + ')' if p.get('ai_reason') else ''}.")
    elif outcome == "RISK_REJECTED":
        head = (f"No trade — V8-D produced {p.get('latest_signal')} on {sym} at {when}, but a risk/safety guard "
                f"blocked it: {p.get('risk_detail') or p.get('execution_detail') or 'risk check rejected'}.")
    elif outcome == "EXECUTION_REJECTED":
        head = (f"No trade — the signal passed the earlier gates but execution was rejected: "
                f"{p.get('execution_detail') or 'see execution detail'}.")
    elif outcome == "MARKET_CLOSED":
        head = "No trade — the market (or entry window) is closed, so V8-D was not evaluated."
    elif outcome == "DATA_ERROR":
        head = (f"No trade — market data was not usable for {sym}, so V8-D was NOT evaluated "
                f"({p.get('signal_detail')}). This is a data problem, not a strategy outcome.")
    elif outcome == "SCANNER_ERROR":
        head = f"No trade — the scan iteration failed ({p.get('signal_detail')}); V8-D was not evaluated."
    elif outcome == "FILLED":
        head = f"A paper trade WAS taken on {sym} at {when}: {p.get('latest_signal')} → FILLED (paper)."
    else:
        head = f"No trade ({outcome})."

    rows = []
    for r in (p.get("symbols") or []):
        extra = f" — {r.get('binding')} failed" if r.get("outcome") == "NO_SIGNAL" and r.get("binding") else ""
        rows.append(f"  - {r.get('symbol')}: {r.get('outcome')}{extra}")
    out = [head] + [b for b in body if b]
    if outcome != "FILLED":
        out.append(chain)
    if len(rows) > 1:
        out.append("Latest scan of each symbol:")
        out += rows
    out.append("(The Copilot chat model only writes explanations — it is separate from the AI Trading Decision gate.)")
    return "\n".join(x for x in out if x)
