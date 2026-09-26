"""AI Trading Decision API — read-only (PHASE 5.1 §9/§10/§18).

No POST/write endpoints: enabling the AI layer is an env change + worker
restart, never a runtime toggle. Reads come from the shared SQLite DB the
worker writes (ai_decisions table + worker scan settings), so this works
whether the worker is in-process or a separate paper_worker.py process.

Everything reported here is REAL state: enabled/disabled, the configured
model, measured latency, stored decisions with their input snapshot hashes,
and the last scan's "why didn't we trade?" breakdown. Nothing is fabricated.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from backend.ai_decision.contract import BACKTEST_UNAVAILABLE
from backend.ai_decision.decision_engine import load_ai_decision_settings

router = APIRouter()


def _shared_db() -> Optional[Any]:
    """The shared DatabaseManager (API process). The ai_decisions table is
    created additively on first access so a fresh API process still works
    when the worker has not written anything yet."""
    try:
        import os
        from backend.database.db_manager import DatabaseManager
        path = os.environ.get("DATABASE_PATH", "data/trading_bot.db")
        db = DatabaseManager(db_path=path)
        db.init_db()
        from backend.ai_decision.store import _SCHEMA
        conn = db._connect()
        conn.executescript(_SCHEMA)
        conn.commit()
        return db
    except Exception:
        return None


@router.get("/status")
def get_ai_decision_status() -> Dict[str, Any]:
    """Configured AI decision layer + measured latency telemetry."""
    settings = load_ai_decision_settings()
    stats: Dict[str, Any] = {}
    recent: List[Dict[str, Any]] = []
    layer_note = ""
    db = _shared_db()
    if db is not None:
        try:
            from backend.ai_decision.store import AIDecisionStore
            store = AIDecisionStore(db)
            stats = store.latency_stats()
            rows = db._connect().execute(
                """SELECT decision_id, signal_id, strategy, symbol, decision, confidence,
                          reason_codes, model_provider, model_name, model_version,
                          input_snapshot_hash, created_at, latency_ms
                   FROM ai_decisions ORDER BY created_at DESC LIMIT 20"""
            ).fetchall()
            for r in rows:
                try:
                    codes = json.loads(r["reason_codes"] or "[]")
                except Exception:
                    codes = []
                recent.append({
                    "decision_id": r["decision_id"],
                    "signal_id": r["signal_id"],
                    "strategy": r["strategy"],
                    "symbol": r["symbol"],
                    "decision": r["decision"],
                    "confidence": r["confidence"],
                    "reason_codes": codes,
                    "model_provider": r["model_provider"],
                    "model_name": r["model_name"],
                    "model_version": r["model_version"],
                    "input_snapshot_hash": r["input_snapshot_hash"],
                    "created_at": r["created_at"],
                    "latency_ms": r["latency_ms"],
                })
            layer_note = db.get_setting("ai_decision_layer", "")
        except Exception:
            pass
    enabled_env = bool(settings["enabled"])
    return {
        "ai_decision_enabled": enabled_env,
        "worker_layer_state": layer_note or None,
        "provider": settings["provider"],
        "model": settings["model"],
        "base_url": settings["base_url"],
        "timeout_seconds": settings["timeout_seconds"],
        "temperature": settings["temperature"],
        "architecture": "V8-D signal → AI decision (APPROVE/REJECT/WAIT) → hard risk → position sizing → execution pipeline",
        "approval_semantics": "AI APPROVE is necessary but never sufficient — hard risk always overrides AI",
        "backtest_status": BACKTEST_UNAVAILABLE,
        "latency": stats,
        "recent_decisions": recent,
        "note": (
            "AI layer enabled — every V8-D BUY is gated by an AI decision before "
            "hard risk. AI failures fail closed to NO TRADE."
            if enabled_env
            else "AI layer disabled — paper trading runs V8-D-only; enable with "
                 "AI_DECISION_ENABLED=true (env change + worker restart)."
        ),
    }


@router.get("/why-not-traded")
def get_why_not_traded() -> Dict[str, Any]:
    """'Why didn't we trade?' — the last scan's gate-by-gate breakdown (§10)."""
    out: Dict[str, Any] = {
        "available": False,
        "reason": "no scan result recorded yet",
    }
    db = _shared_db()
    if db is None:
        return out
    try:
        raw = db.get_setting("paper_worker_last_scan_detail", "")
        if not raw:
            return out
        detail = json.loads(raw)
        out["available"] = True
        out["reason"] = detail.get("reason")
        out["scanned"] = detail.get("scanned")
        out["traded"] = detail.get("traded")
        out["signal"] = detail.get("signal")
        details = detail.get("details") or {}
        breakdown: Dict[str, Any] = {
            "v8_d": "PASS" if detail.get("signal") == "BUY" else "REJECTED",
            "v8_d_rejection_reasons": details.get("rejection") or [],
            "ai_decision": details.get("ai_decision"),
            "ai_confidence": details.get("ai_confidence"),
            "ai_reason_codes": details.get("ai_reason_codes") or [],
            "ai_decision_id": details.get("ai_decision_id"),
            "ai_model": details.get("ai_model"),
        }
        reason = str(detail.get("reason") or "")
        # Map scan reasons to the §10 taxonomy — honest, derived only from
        # what the scanner actually recorded.
        if reason.startswith("AI_NO_TRADE:"):
            code = reason.split(":", 1)[1]
            if code in ("AI_WAIT",) or "STALE_DATA" in code or "INCOMPLETE_CONTRACT" in code:
                breakdown["stage"] = "AI_WAITING"
            else:
                breakdown["stage"] = "AI_REJECTED"
        elif reason.startswith("AI_STRATEGY_MISMATCH"):
            breakdown["stage"] = "AI_REJECTED"
        elif reason == "market_closed":
            breakdown["stage"] = "MARKET_CLOSED"
        elif reason.startswith("stale_candles") or reason.startswith("insufficient_candles"):
            breakdown["stage"] = "STALE_OR_INSUFFICIENT_DATA"
        elif reason.startswith("rejected:kill_switch"):
            breakdown["stage"] = "KILL_SWITCH"
        elif reason.startswith("rejected:MAX_") or reason.startswith("rejected:INSUFFICIENT"):
            breakdown["stage"] = "RISK_REJECTED"
        elif reason.startswith("rejected:INVALID_LOT"):
            breakdown["stage"] = "NO_VALID_LOT_SIZE"
        elif reason.startswith("rejected:INVALID_CONTRACT"):
            breakdown["stage"] = "NO_VALID_CONTRACT"
        elif reason.startswith("rejected:"):
            breakdown["stage"] = "EXECUTION_REJECTED"
            breakdown["execution_reason"] = reason.split(":", 1)[1]
        elif detail.get("traded"):
            breakdown["stage"] = "TRADED"
        elif reason.startswith("no_trade:"):
            breakdown["stage"] = "V8D_REJECTED"
        else:
            breakdown["stage"] = "NO_SIGNAL_OR_DATA"
        out["breakdown"] = breakdown
        return out
    except Exception as exc:
        return {"available": False, "reason": f"unreadable scan state: {type(exc).__name__}"}


@router.get("/decisions/{decision_id}")
def get_ai_decision_detail(decision_id: str) -> Dict[str, Any]:
    """Full stored decision by id — reproducibility for 'why did the AI
    approve this?' (§7). Includes the input snapshot hash (the snapshot
    itself is rebuildable from the stored hash + recorded context)."""
    db = _shared_db()
    if db is None:
        return {"available": False, "reason": "database unavailable"}
    try:
        from backend.ai_decision.store import AIDecisionStore
        store = AIDecisionStore(db)
        stored = store.get_decision(decision_id)
        if stored is None:
            return {"available": False, "reason": "decision_id not found"}
        return {"available": True, "decision": stored}
    except Exception as exc:
        return {"available": False, "reason": f"lookup failed: {type(exc).__name__}"}
