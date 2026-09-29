"""PHASE B — ONE authoritative "WHY DIDN'T WE TRADE?" gate chain.

The full trade decision chain (spec §8):

    MARKET GATE → DATA GATE → V8-D SIGNAL → AI DECISION → HARD RISK →
    POSITION SIZING → CONTRACT VALIDATION → EXECUTION PIPELINE →
    BROKER/PAPER EXECUTION → RECONCILIATION

This module derives that chain from the ONE persisted scan record the paper
worker writes every tick (`settings.paper_worker_last_scan_detail`, written
in backend/paper/paper_worker.py from the PaperMarketScanner ScanResult).
Previously TWO independent mappings of that record existed:

  1. backend/api/routers/ai_decision.py:get_why_not_traded  (frontend panel)
  2. ad-hoc guessing inside Copilot context builders

…which drifted. This module is now the ONE authority; the ai_decision router
delegates to it and the Copilot context embeds the same chain, so the panel,
Copilot, and Operations can never disagree.

Every gate carries `status` (OK | REJECTED | NOT_EVALUATED | SKIPPED |
UNKNOWN) plus `detail`. `final_reason` uses the typed taxonomy:

  NO_SIGNAL · SIGNAL_REJECTED · AI_REJECTED · AI_TIMEOUT · AI_WAITING ·
  RISK_REJECTED · SIZING_REJECTED · NO_VALID_CONTRACT · NO_VALID_LOT_SIZE ·
  EXECUTION_REJECTED · MARKET_CLOSED · RECONCILIATION_NOT_READY ·
  RECONCILIATION_STALE · KILL_SWITCH · MAX_TRADES_REACHED ·
  MAX_EXPOSURE_REACHED · STALE_DATA · TRADED

NO HALLUCINATION RULE: when nothing has been persisted yet this returns
{"available": False, "reason": ...} — it NEVER fabricates a chain.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _gate(status: str, detail: Any = None) -> Dict[str, Any]:
    return {"status": status, "detail": detail}


def _stage_from_reason(reason: str) -> str:
    """Map a persisted scan reason to the typed stage taxonomy.

    Mirrors the ai_decision router mapping exactly — this module is the
    authority; the router delegates here.
    """
    r = str(reason or "")
    if r.startswith("AI_NO_TRADE:"):
        code = r.split(":", 1)[1]
        if code == "AI_TIMEOUT":
            return "AI_TIMEOUT"
        if code in ("AI_PROVIDER_UNAVAILABLE", "AI_MODEL_UNAVAILABLE"):
            return code
        if code in ("AI_INVALID_RESPONSE", "AI_DECISION_INVALID"):
            return "AI_INVALID_RESPONSE"
        if code == "AI_DECISION_PERSISTENCE_FAILED":
            return "AI_DECISION_PERSISTENCE_FAILED"
        if code == "RECONCILIATION_NOT_READY":
            return "RECONCILIATION_NOT_READY"
        if code == "RECONCILIATION_STALE":
            return "RECONCILIATION_STALE"
        if code == "AI_WAITING":
            return "AI_WAITING"
        if code in ("AI_WAIT",) or "STALE_DATA" in code or "INCOMPLETE_CONTRACT" in code:
            return "AI_WAITING"
        return "AI_REJECTED"
    if r.startswith("AI_STRATEGY_MISMATCH"):
        return "AI_REJECTED"
    if r == "market_closed":
        return "MARKET_CLOSED"
    if r.startswith("entry_window_closed") or r.startswith("entry_window_error"):
        return "MARKET_CLOSED"
    if r.startswith("stale_candles") or r.startswith("insufficient_candles"):
        return "STALE_DATA"
    if r.startswith("rejected:kill_switch"):
        return "KILL_SWITCH"
    if r.startswith("rejected:MAX_DAILY_TRADES"):
        return "MAX_TRADES_REACHED"
    if r.startswith("rejected:MAX_POSITIONS"):
        return "MAX_EXPOSURE_REACHED"
    if r.startswith("rejected:MAX_DAILY_LOSS"):
        return "RISK_REJECTED"
    if r.startswith("rejected:INSUFFICIENT_EQUITY"):
        return "SIZING_REJECTED"
    if r.startswith("rejected:MAX_") or r.startswith("rejected:INSUFFICIENT"):
        return "RISK_REJECTED"
    if r.startswith("rejected:INVALID_LOT"):
        return "NO_VALID_LOT_SIZE"
    if r.startswith("rejected:INVALID_CONTRACT"):
        return "NO_VALID_CONTRACT"
    if r.startswith(("submit_error:", "candle_fetch_error:", "chain_fetch_error:",
                     "expiry_fetch_error:", "scan_error:")):
        return "BROKER_UNAVAILABLE"
    if r.startswith("rejected:"):
        if "BROKER" in r.upper():
            return "BROKER_REJECTED"
        return "EXECUTION_REJECTED"
    if r.startswith("no_trade:"):
        return "SIGNAL_REJECTED"
    if r in ("", None):
        return "UNKNOWN"
    return "NO_SIGNAL_OR_DATA"


def build_gate_chain_from_detail(detail: Dict[str, Any]) -> Dict[str, Any]:
    """Build the full gate chain from ONE persisted scan detail dict.

    `detail` is the payload the paper worker persisted:
    {"scanned", "traded", "reason", "signal", "details": {...}}
    """
    inner = detail.get("details") or {}
    reason = str(detail.get("reason") or "")
    signal = detail.get("signal")
    traded = bool(detail.get("traded"))
    stage = "TRADED" if traded else _stage_from_reason(reason)

    chain: Dict[str, Any] = {}
    chain["market"] = (
        _gate("OK", "market_closed reason recorded" ) if stage == "MARKET_CLOSED"
        else _gate("OK", "scan passed the market/session gate") if detail.get("scanned")
        else _gate("SKIPPED", "scan did not run")
    )
    data_detail = inner.get("bars")
    chain["data"] = (
        _gate("REJECTED", reason) if stage == "STALE_DATA"
        else _gate("OK", {"bars": data_detail} if data_detail else "candles fresh")
    )
    chain["v8d_signal"] = (
        _gate("OK", "BUY") if signal == "BUY"
        else _gate("REJECTED", inner.get("rejection") or reason) if detail.get("scanned")
        else _gate("NOT_EVALUATED", "no scan")
    )
    ai_decision = inner.get("ai_decision")
    if ai_decision:
        ai_status = "OK" if ai_decision == "APPROVE" else "REJECTED"
        chain["ai_decision"] = _gate(ai_status, {
            "decision": ai_decision,
            "confidence": inner.get("ai_confidence"),
            "reason_codes": inner.get("ai_reason_codes") or [],
            "decision_id": inner.get("ai_decision_id"),
            "model": inner.get("ai_model"),
        })
    else:
        chain["ai_decision"] = _gate(
            "NOT_EVALUATED" if signal != "BUY" else "SKIPPED",
            "AI layer disabled or not reached" if signal == "BUY"
            else "V8-D produced no BUY to evaluate",
        )
    risk_status = "REJECTED" if stage in (
        "MAX_TRADES_REACHED", "MAX_EXPOSURE_REACHED", "RISK_REJECTED", "KILL_SWITCH",
    ) else ("OK" if traded else "NOT_EVALUATED")
    chain["hard_risk"] = _gate(risk_status, reason if risk_status == "REJECTED" else None)
    sizing_status = "REJECTED" if stage == "SIZING_REJECTED" else ("OK" if traded else "NOT_EVALUATED")
    chain["position_sizing"] = _gate(sizing_status, reason if sizing_status == "REJECTED" else None)
    contract_status = "REJECTED" if stage in ("NO_VALID_CONTRACT", "NO_VALID_LOT_SIZE") else (
        "OK" if traded else "NOT_EVALUATED")
    chain["contract_validation"] = _gate(
        contract_status, reason if contract_status == "REJECTED" else None)
    exec_status = "REJECTED" if stage in (
        "EXECUTION_REJECTED", "BROKER_REJECTED", "BROKER_UNAVAILABLE",
    ) else ("OK" if traded else "NOT_ATTEMPTED")
    chain["execution_pipeline"] = _gate(exec_status, reason if exec_status == "REJECTED" else None)
    broker_status = "OK" if traded else (
        "REJECTED" if stage in ("BROKER_REJECTED", "BROKER_UNAVAILABLE") else "NOT_ATTEMPTED")
    chain["broker_paper_execution"] = _gate(
        broker_status, reason if broker_status == "REJECTED" else None)
    rec_detail = inner.get("reconciliation_age_seconds")
    rec_status = (
        "REJECTED" if stage in ("RECONCILIATION_NOT_READY", "RECONCILIATION_STALE")
        else "UNKNOWN"
    )
    chain["reconciliation"] = _gate(
        rec_status,
        {"age_seconds": rec_detail} if rec_detail is not None else "verdict age unknown",
    )

    return {
        "available": True,
        "stage": stage,
        "final_reason": stage if not traded else "TRADED",
        "scanned": bool(detail.get("scanned")),
        "traded": traded,
        "scan_reason": reason or None,
        "signal": signal,
        "v8d_rejection_reasons": list(inner.get("rejection") or []),
        "ai_decision": ai_decision,
        "gates": chain,
        "human_summary": _human_summary(stage, reason, inner, signal, traded),
    }


def _human_summary(
    stage: str, reason: str, inner: Dict[str, Any],
    signal: Any, traded: bool,
) -> str:
    if traded:
        return "A trade WAS executed this scan (paper fill confirmed)."
    if stage == "MARKET_CLOSED":
        return "The bot did not trade because the market/session gate is closed " \
               f"({reason or 'outside trading hours'})."
    if stage == "STALE_DATA":
        return f"The bot did not trade because market data is stale or insufficient ({reason})."
    if stage == "SIGNAL_REJECTED" or stage == "NO_SIGNAL":
        reasons = inner.get("rejection") or []
        base = "V8-D produced no actionable BUY signal this scan"
        return base + (f": {'; '.join(str(x) for x in reasons[:3])}" if reasons else ".")
    if stage == "AI_REJECTED":
        return "V8-D produced a BUY, but the AI trading decision layer REJECTED it" + (
            f" ({', '.join(str(c) for c in (inner.get('ai_reason_codes') or []))})"
            if inner.get("ai_reason_codes") else ".")
    if stage == "AI_TIMEOUT":
        return "V8-D produced a BUY, but the AI decision did not return inside its budget — fail-closed NO TRADE."
    if stage == "AI_WAITING":
        return "V8-D produced a BUY; the AI decision is still pending (bounded budget) and will replay on a later scan."
    if stage == "MAX_TRADES_REACHED":
        return "V8-D produced a BUY, but the configured max trades per day was already reached — hard risk rejected the entry."
    if stage == "MAX_EXPOSURE_REACHED":
        return "V8-D produced a BUY, but the max concurrent positions limit was reached."
    if stage == "RISK_REJECTED":
        return f"V8-D produced a BUY, but the hard risk gate rejected it ({reason})."
    if stage == "SIZING_REJECTED":
        return f"Position sizing rejected the trade ({reason})."
    if stage in ("NO_VALID_CONTRACT", "NO_VALID_LOT_SIZE"):
        return f"Contract/lot validation failed ({reason})."
    if stage in ("RECONCILIATION_NOT_READY", "RECONCILIATION_STALE"):
        return f"Trading is blocked by reconciliation state ({stage.lower()})."
    if stage == "KILL_SWITCH":
        return "The kill switch is ACTIVE — all new entries are blocked."
    if stage in ("BROKER_REJECTED", "BROKER_UNAVAILABLE"):
        return f"Execution could not complete against the broker/paper book ({reason})."
    if stage == "EXECUTION_REJECTED":
        return f"The execution pipeline rejected the entry ({reason})."
    if stage == "TRADED":
        return "A trade WAS executed this scan (paper fill confirmed)."
    return f"No trade this scan (stage={stage}, reason={reason or 'n/a'})."


def build_gate_chain_from_db(db: Any) -> Dict[str, Any]:
    """Read the persisted scan detail and build the chain.

    Honest empty state (spec §25/§30): when nothing has been persisted yet,
    returns available=False with the EXACT reason — never a fabricated chain.
    """
    try:
        raw = db.get_setting("paper_worker_last_scan_detail", "") or ""
    except Exception as exc:
        return {"available": False,
                "reason": f"scan state unreadable ({type(exc).__name__})",
                "stage": "UNKNOWN", "gates": {}, "human_summary":
                "The bot's last scan state could not be read from the database."}
    if not raw:
        return {
            "available": False,
            "reason": "No scan has been recorded since bot startup — the paper "
                      "worker persists every scan to paper_worker_last_scan_detail; "
                      "start the bot/worker and wait for the first scan tick.",
            "stage": "UNKNOWN",
            "gates": {},
            "human_summary": "No scan has been recorded since bot startup.",
        }
    try:
        detail = json.loads(raw)
    except Exception:
        return {"available": False, "reason": "persisted scan detail is corrupt JSON",
                "stage": "UNKNOWN", "gates": {},
                "human_summary": "The last scan record could not be parsed."}
    chain = build_gate_chain_from_detail(detail if isinstance(detail, dict) else {})
    # Age of the record (freshness — spec §28).
    checked = None
    try:
        inner = detail.get("details") or {}
        checked = inner.get("recorded_at")
    except Exception:
        checked = None
    if not checked:
        try:
            checked = db.get_setting("paper_worker_last_scan_ts", "") or None
        except Exception:
            checked = None
    if checked:
        try:
            ca = datetime.fromisoformat(str(checked))
            if ca.tzinfo is None:
                ca = ca.replace(tzinfo=timezone.utc)
            chain["age_seconds"] = round(max(0.0, (
                datetime.now(timezone.utc) - ca).total_seconds()), 1)
            chain["recorded_at"] = ca.isoformat()
        except Exception:
            pass
    return chain
