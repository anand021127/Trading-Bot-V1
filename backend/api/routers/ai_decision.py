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
    # PHASE 5.3 QA: report the AUTHORITATIVE runtime state, not just the env
    # default. The runtime AI toggle (POST /api/bot/ai-toggle) writes a DB
    # override that the scan loop re-reads EVERY tick — the status endpoint
    # previously reported env-only, so the UI badge could contradict the
    # actual gating (e.g. env=false + override="1" showed DISABLED while AI
    # was genuinely gating every signal). Same authority chain as
    # bot_control.ai_effectively_enabled: override "1"/"0" wins, env default
    # otherwise, fail-closed OFF when the DB is unreadable.
    enabled_effective = enabled_env
    try:
        from backend.api.routers.bot_control import AI_ENABLED_OVERRIDE_KEY
        if db is not None:
            override = str(db.get_setting(AI_ENABLED_OVERRIDE_KEY, "") or "")
            if override == "1":
                enabled_effective = True
            elif override == "0":
                enabled_effective = False
    except Exception:
        pass
    counters: Dict[str, int] = {}
    if db is not None:
        try:
            rows = db._connect().execute(
                "SELECT decision, COUNT(*) AS n FROM ai_decisions GROUP BY decision"
            ).fetchall()
            for r in rows:
                counters[str(r["decision"]).lower()] = int(r["n"])
            fail_rows = db._connect().execute(
                """SELECT reason_codes, COUNT(*) AS n FROM ai_decisions
                   WHERE decision='REJECT' GROUP BY reason_codes ORDER BY n DESC LIMIT 10"""
            ).fetchall()
            fail_counts: Dict[str, int] = {}
            for r in fail_rows:
                try:
                    codes = json.loads(r["reason_codes"] or "[]")
                except Exception:
                    codes = []
                if codes:
                    fail_counts[codes[0]] = fail_counts.get(codes[0], 0) + int(r["n"])
            counters["rejection_breakdown"] = fail_counts  # type: ignore[assignment]
        except Exception:
            pass
    return {
        "ai_decision_enabled": enabled_effective,
        "env_default_enabled": enabled_env,
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
        "decision_counters": counters,
        "recent_decisions": recent,
        "note": (
            "AI layer enabled — every V8-D BUY is gated by an AI decision before "
            "hard risk. AI failures fail closed to NO TRADE."
            if enabled_effective
            else "AI layer disabled — paper trading runs V8-D-only; enable with "
                 "the AI toggle on /operations (runtime override) or "
                 "AI_DECISION_ENABLED=true (env change + worker restart)."
        ),
    }


@router.get("/why-not-traded")
def get_why_not_traded() -> Dict[str, Any]:
    """'Why didn't we trade?' — the last scan's gate-by-gate breakdown (§10).

    PHASE B: this endpoint DELEGATES to backend/copilot/gate_chain.py — the
    ONE authoritative gate-chain builder (also embedded into the Copilot
    context and Operations), so the panel and the Copilot can never
    disagree about why the last scan did not trade.
    """
    out: Dict[str, Any] = {
        "available": False,
        "reason": "no scan result recorded yet",
    }
    db = _shared_db()
    if db is None:
        return out
    try:
        from backend.copilot.gate_chain import build_gate_chain_from_db
        chain = build_gate_chain_from_db(db)
        if not chain.get("available"):
            # Persist the honest reason (startup-empty state, corrupt row…)
            # instead of the generic default above when we know more.
            if chain.get("reason"):
                out["reason"] = chain["reason"]
            return out
        out["available"] = True
        out["scanned"] = chain.get("scanned")
        out["traded"] = chain.get("traded")
        out["signal"] = chain.get("signal")
        out["reason"] = chain.get("scan_reason")
        out["stage"] = chain.get("stage")
        out["gates"] = chain.get("gates") or {}
        out["human_summary"] = chain.get("human_summary")
        out["diagnostics"] = chain.get("diagnostics") or {}
        if chain.get("recorded_at"):
            out["recorded_at"] = chain["recorded_at"]
        if chain.get("age_seconds") is not None:
            out["age_seconds"] = chain["age_seconds"]
        # Legacy `breakdown` shape (Copilot.tsx AIDecisionPanel contract).
        inner_raw = db.get_setting("paper_worker_last_scan_detail", "") or ""
        details: Dict[str, Any] = {}
        try:
            details = (json.loads(inner_raw) or {}).get("details") or {}
        except Exception:
            details = {}
        out["breakdown"] = {
            "stage": chain.get("stage"),
            # V8-D is only "REJECTED" when it was actually evaluated; a data or
            # scanner failure means it was NOT evaluated (different problem).
            "v8_d": ("PASS" if chain.get("signal") == "BUY"
                     else "NOT_EVALUATED" if (chain.get("stage") in (
                         "STALE_DATA", "DATA_UNAVAILABLE", "SCANNER_ERROR", "MARKET_CLOSED")
                         or (chain.get("gates", {}).get("v8d_signal", {}).get("status")
                             == "NOT_EVALUATED"))
                     else "REJECTED"),
            "v8_d_rejection_reasons": chain.get("v8d_rejection_reasons") or [],
            "ai_decision": details.get("ai_decision"),
            "ai_confidence": details.get("ai_confidence"),
            "ai_reason_codes": details.get("ai_reason_codes") or [],
            "ai_decision_id": details.get("ai_decision_id"),
            "ai_model": details.get("ai_model"),
            "execution_reason": chain.get("scan_reason"),
        }
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
