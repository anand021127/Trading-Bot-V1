"""Bot control endpoints — start, stop, kill switch, mode/AI control, and status."""
from __future__ import annotations

import asyncio
import logging
import os
from types import SimpleNamespace
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.api.control_auth import require_control_token
from backend.config.runtime_config import get_effective_settings
from backend.config.settings import load_settings
from backend.strategy.trading_engine import BotState

# State-changing control endpoints: guarded by the optional control token
# (no-op unless CONTROL_TOKEN is set — see backend/api/control_auth.py).
router = APIRouter(dependencies=[Depends(require_control_token)])
settings = load_settings()


def _settings_now():
    """PHASE B: authoritative settings (Settings-DB blob over env), re-read
    per call so a Settings-UI save reaches Operations/mode gates without a
    process restart. `settings` above is retained only for legacy importers
    and tests that patch it.

    Legacy in-process override compat: code/tests that mutate the module-global
    `settings.mode` AFTER import (e.g. test_single_execution_path) intend an
    immediate override — the effective snapshot would silently shadow it. When
    the global diverges from a fresh env load, honor it. In production the
    global is never mutated, so effective (blob-over-env) config always wins.
    """
    try:
        s_eff = get_effective_settings()
        try:
            _base = load_settings()
            # Legacy in-process `mode` override only fills in when the saved
            # blob did not set a mode (saved-blob > in-process > env default).
            if getattr(settings, "mode", None) and \
                    settings.mode != getattr(_base, "mode", None) and \
                    s_eff.mode == getattr(_base, "mode", None):
                s_eff.mode = settings.mode
        except Exception:
            pass
        return s_eff
    except Exception:
        return settings
logger = logging.getLogger(__name__)

# Shared engine reference — set by main.py at startup
_engine_ref: Any = None
_paper_runtime_ref: Any = None


def set_engine(engine: Any) -> None:
    global _engine_ref
    _engine_ref = engine


def get_engine() -> Any:
    """Return the shared TradingEngine instance (if initialized)."""
    return _engine_ref


def set_paper_runtime(runtime: Any) -> None:
    global _paper_runtime_ref
    _paper_runtime_ref = runtime


def get_paper_runtime() -> Any:
    return _paper_runtime_ref


def _paper_runtime_from_app() -> Optional[Any]:
    if _paper_runtime_ref is not None:
        return _paper_runtime_ref
    try:
        import backend.api.main as main_mod
        rt = getattr(getattr(main_mod, "app", None), "state", None)
        rt = getattr(rt, "paper_runtime", None) if rt is not None else None
        if rt is not None:
            return rt
    except Exception:
        pass
    # Offline test/offline API context: no runtime is constructed. Synthesize
    # a minimal handle around the shared DatabaseManager so DB-backed control
    # endpoints (AI toggle, mode request, kill-switch persistence) still work
    # against real settings rows instead of refusing.
    try:
        from backend.database.db_manager import DatabaseManager
        return SimpleNamespace(_synthetic_db_stub=True, db=DatabaseManager(
            db_path=os.environ.get("DATABASE_PATH", "data/trading_bot.db")))
    except Exception:
        return None


def paper_runtime_state() -> Optional[Dict[str, Any]]:
    """REAL operational state of the paper scanner (worker process alive +
    fresh heartbeat + persisted scan record), or None outside paper mode /
    when the state DB is unreadable. See backend/paper/scan_state.py."""
    try:
        from backend.paper.scan_state import compute_runtime_state
        return compute_runtime_state(BotState._get_db())
    except Exception:  # noqa: BLE001
        return None


# ── PHASE 5.3 §8: server-side AI toggle ─────────────────────────────────
# The frontend must never edit .env to flip AI. This DB-backed override is
# read on EVERY scan (market_scan_loop) so a toggle takes effect within one
# tick — no worker restart — while the env var remains the default.
AI_ENABLED_OVERRIDE_KEY = "ai_decision_enabled_override"  # "1"/"0"/""


def ai_effectively_enabled(db: Any, engine: Any = None) -> bool:
    """AI gate authority: DB override (set via the control API) wins over
    the env-configured engine default. Fail-closed: absent everything → OFF."""
    try:
        override = str(db.get_setting(AI_ENABLED_OVERRIDE_KEY, "") or "") if db is not None else ""
    except Exception:
        override = ""
    if override == "1":
        return True
    if override == "0":
        return False
    return bool(getattr(engine, "enabled", False))


class AIToggleRequest(BaseModel):
    enabled: bool


class ModeSwitchRequest(BaseModel):
    mode: str  # "paper" | "live"


@router.get("/operations")
async def operations_dashboard() -> Dict[str, Any]:
    """ONE payload for the TRADING CONTROL dashboard (PHASE 5.3 §30):
    mode, strategy, AI, broker, market, API/data health, reconciliation
    (with age), kill switch, and the LIVE readiness verdict with exact
    blocked reasons. Read-only — no state is changed by a GET."""
    from datetime import datetime, timezone

    db = None
    paper_rt = _paper_runtime_from_app()
    if paper_rt is not None:
        db = getattr(paper_rt, "db", None)

    # AI layer state (engine may live in the worker process; env + override
    # + persisted worker note are the cross-process truth).
    ai_enabled = ai_effectively_enabled(db, None)
    ai_note = None
    try:
        if db is not None:
            ai_note = db.get_setting("ai_decision_layer", "") or None
    except Exception:
        pass
    ai_status: Dict[str, Any] = {
        "enabled": ai_enabled,
        "override": (str(db.get_setting(AI_ENABLED_OVERRIDE_KEY, "") or "") if db is not None else ""),
        "worker_layer_state": ai_note,
    }
    try:
        from backend.ai_decision.decision_engine import load_ai_decision_settings
        s = load_ai_decision_settings()
        ai_status.update({"provider": s["provider"], "model": s["model"],
                          "base_url": s["base_url"],
                          "timeout_seconds": s["timeout_seconds"]})
    except Exception:
        pass

    # Reconciliation state + age (real values, never defaults).
    recon: Dict[str, Any] = {"state": "unknown", "age_seconds": None}
    if db is not None:
        try:
            from backend.paper.market_scan_loop import read_reconciliation_state
            state, detail, age = read_reconciliation_state(SimpleNamespace(db=db))
            recon = {"state": ("OK" if state == "1" else "FAILED" if state == "0" else "NEVER_CHECKED"),
                     "age_seconds": (round(age, 1) if age is not None else None),
                     "detail": detail}
        except Exception as exc:  # noqa: BLE001
            recon = {"state": "unknown", "error": type(exc).__name__}

    # Kill switch level (backend-enforced, not just BotState flag).
    kill_level = "UNKNOWN"
    if paper_rt is not None and hasattr(paper_rt, "kill"):
        try:
            kill_level = str(paper_rt.kill.level())
        except Exception:
            pass

    # Market session (authoritative exchange calendar).
    market = {"status": "UNKNOWN"}
    try:
        from backend.market.calendar import exchange_calendar
        status, note = exchange_calendar.session_status()
        market = {"status": status, "note": note}
    except Exception as exc:  # noqa: BLE001
        market = {"status": "UNKNOWN", "error": type(exc).__name__}

    # API + data health from the health monitor / instrument master.
    api_health: Dict[str, Any] = {}
    try:
        from backend.health.health_monitor import health_monitor
        api_health = health_monitor.snapshot()
    except Exception:
        pass
    data_health: Dict[str, Any] = {}
    try:
        from backend.broker.instrument_master import get_master_status
        data_health = get_master_status()
    except Exception:
        pass

    # LIVE readiness verdict (§10) — evaluated for display on every call.
    live_verdict: Dict[str, Any] = {"ready": False, "blocked_reasons": ["not_evaluated"]}
    try:
        from backend.execution.live_gate import evaluate_live_readiness
        client = getattr(_engine_ref, "client", None) if _engine_ref is not None else None
        verdict = evaluate_live_readiness(client=client, db=db, settings=_settings_now(),
                                          runtime=_engine_ref)
        live_verdict = verdict.to_dict()
    except Exception as exc:  # noqa: BLE001
        live_verdict = {"ready": False, "blocked_reasons": [f"gate_error:{type(exc).__name__}"]}

    # PHASE B — authoritative runtime configuration + mismatch warnings.
    # Operations must agree with Settings/Overview/Copilot: this is the SAME
    # DB-over-env resolution the trading engine uses, with provenance labels
    # (sqlite_settings / env_TRADING_CAPITAL / default) per key.
    runtime_config: Dict[str, Any] = {}
    try:
        from backend.config.runtime_config import runtime_config_summary
        runtime_config = runtime_config_summary()
    except Exception as exc:  # noqa: BLE001
        runtime_config = {"available": False, "error": type(exc).__name__}

    return {
        "mode": (_settings_now().mode or "paper").lower(),
        "strategy": os.environ.get("TRADING_STRATEGY", "V8_D_PULLBACK_ATM"),
        "broker": "UPSTOX",
        "market": market,
        "api_health": api_health,
        "data_health": data_health,
        "reconciliation": recon,
        "kill_switch": {"level": kill_level,
                        "triggered": kill_level != "OFF" or BotState.status()["kill_switch_active"]},
        "ai": ai_status,
        "live_readiness": live_verdict,
        "runtime_config": runtime_config,
        "bot_running": BotState.is_running(),
        "paper_runtime_state": paper_runtime_state(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/ai-toggle")
async def ai_toggle(request: AIToggleRequest) -> Dict[str, Any]:
    """PHASE 5.3 §8 — enable/disable the AI trading decision layer at
    runtime. Server-side authority: the value is persisted in the DB and
    read by the scan gate each tick. AI ON never bypasses hard risk — the
    gate sits BEFORE RiskManager/Sizer in the chain, never instead of it."""
    db = None
    paper_rt = _paper_runtime_from_app()
    if paper_rt is not None:
        db = getattr(paper_rt, "db", None)
    if db is None:
        return {"success": False, "message": "No runtime DB attached — cannot persist AI toggle"}
    db.save_setting(AI_ENABLED_OVERRIDE_KEY, "1" if request.enabled else "0")
    return {"success": True, "ai_enabled": request.enabled,
            "message": ("AI trading decision ENABLED — every V8-D BUY is now "
                        "AI-gated before hard risk (fail-closed)."
                        if request.enabled else
                        "AI trading decision DISABLED — V8-D → hard risk directly.")}


@router.post("/mode")
async def switch_mode(request: ModeSwitchRequest) -> Dict[str, Any]:
    """PHASE 5.3 §9/§10 — mode switch with server-side validation.

    PAPER: always allowed (paper behavior unchanged).
    LIVE:  refused unless evaluate_live_readiness() reports EVERY mandatory
    condition OK. The readiness gate is backend-enforced — the frontend can
    only display it. Even when ready, the actual worker arming requires an
    operator restart with TRADING_MODE=live (documented in the runbook); a
    UI click alone can never arm live trading."""
    requested = (request.mode or "").strip().lower()
    if requested not in ("paper", "live"):
        return {"success": False, "message": f"Unsupported mode {requested!r} (paper|live)"}

    paper_rt = _paper_runtime_from_app()
    db = getattr(paper_rt, "db", None) if paper_rt is not None else None

    if requested == "paper":
        try:
            if db is not None:
                db.save_setting("trading_mode_request", "paper")
        except Exception:
            pass
        return {"success": True, "mode": "paper",
                "message": "Paper mode requested. Restart the worker with TRADING_MODE=paper "
                           "(the default) if a live worker is running."}

    # requested == "live" — evaluate every gate against REAL state.
    from backend.execution.live_gate import evaluate_live_readiness
    client = getattr(_engine_ref, "client", None) if _engine_ref is not None else None
    verdict = evaluate_live_readiness(client=client, db=db, settings=_settings_now(),
                                      runtime=_engine_ref)
    if not verdict.ready:
        return {"success": False, "mode": "live", "ready": False,
                "blocked_reasons": verdict.blocked_reasons,
                "checks": verdict.checks,
                "message": "LIVE BLOCKED — " + ", ".join(verdict.blocked_reasons)}
    try:
        if db is not None:
            db.save_setting("trading_mode_request", "live")
    except Exception:
        pass
    return {"success": True, "mode": "live", "ready": True,
            "checks": verdict.checks,
            "message": "LIVE READY — all gates passed. Operator must restart the worker "
                       "with TRADING_MODE=live to arm live execution (two-step arming; "
                       "a UI click alone never arms live)."}


@router.get("/status")
async def bot_status() -> Dict[str, Any]:
    """Full bot status for dashboard — includes health model."""
    state = BotState.status()
    risk_status: Dict[str, Any] = {}
    if _engine_ref is not None:
        try:
            risk_status = _engine_ref.risk_manager.get_status()
        except Exception:
            pass

    health_snapshot: Dict[str, Any] = {}
    scanner_health: Dict[str, Any] = {}
    ws_health: Dict[str, Any] = {}
    supervisor_status: Dict[str, Any] = {}
    try:
        from backend.health.health_monitor import health_monitor
        health_snapshot = health_monitor.snapshot()
    except Exception:
        pass

    try:
        import backend.api.routers.scanner as scanner_module
        scanner = scanner_module._scanner_ref
        if scanner is not None:
            scanner_health = scanner.health_report()
    except Exception:
        pass

    try:
        from backend.api.websocket import get_broker_ws_status
        ws_health = get_broker_ws_status()
    except Exception:
        pass

    try:
        import backend.api.main as main_mod
        sup = getattr(getattr(main_mod, "app", None), "state", None)
        sup = getattr(sup, "supervisor", None)
        if sup is not None:
            supervisor_status = sup.status()
    except Exception:
        pass

    paper_rt = _paper_runtime_from_app()
    runtime_state = paper_runtime_state() if (settings.mode or "").lower() == "paper" else None
    return {
        **state,
        # Flag says what was REQUESTED; runtime_state says what is actually
        # HAPPENING (worker alive, scans executing). Never show RUNNING from
        # the flag alone.
        "runtime_state": (runtime_state or {}).get("state"),
        "runtime_label": (runtime_state or {}).get("label"),
        "runtime_summary": (runtime_state or {}).get("summary"),
        "runtime": runtime_state,
        "effective_running": bool(runtime_state and str(runtime_state.get("state", "")).startswith("RUNNING")),
        "mode": settings.mode,
        "risk": risk_status,
        "health": health_snapshot,
        "scanner_health": scanner_health,
        "websocket_health": ws_health,
        "supervisor": supervisor_status,
        "paper_runtime_attached": paper_rt is not None,
        "active_executor": "PaperTradingRuntime" if (settings.mode == "paper") else "TradingEngine",
    }


@router.post("/start")
async def start_bot() -> Dict[str, Any]:
    """Start the trading bot.

    Paper mode starts ONLY PaperTradingRuntime (never TradingEngine.run_forever).
    Live mode is not enabled from this task and remains blocked at the mode gate.
    """
    mode = (_settings_now().mode or "").lower()
    if BotState.is_running():
        # The flag can be stuck "running" after a crash/restart while NO worker
        # exists (the exact "RUNNING but nothing scans" state). Only refuse when
        # the runtime is genuinely up; otherwise fall through and repair.
        _rs = paper_runtime_state() if mode == "paper" else None
        if not (_rs and _rs.get("state") == "STARTED_WORKER_NOT_RESPONDING"):
            return {"success": False, "message": "Bot is already running",
                    "runtime_state": (_rs or {}).get("state")}
    if BotState.status()["kill_switch_active"]:
        return {"success": False, "message": "Kill switch is active. Reset it first via /bot/reset-kill"}

    if mode == "paper":
        paper_rt = _paper_runtime_from_app()
        # A synthesized offline DB stub is NOT a runnable executor: START must
        # refuse exactly as when no runtime is attached at all. (isinstance
        # guard: MagicMock would auto-create any getattr'd flag as truthy.)
        if paper_rt is None or (
            isinstance(paper_rt, SimpleNamespace)
            and getattr(paper_rt, "_synthetic_db_stub", False)
        ):
            return {
                "success": False,
                "message": "PaperTradingRuntime not attached. Refusing to start TradingEngine in paper mode.",
            }
        # Paper runtime is already constructed at lifespan; mark BotState running only.
        # Do NOT call TradingEngine.start()/run_forever in paper mode.
        BotState.start()
        logger.info("Paper START — PaperTradingRuntime only (TradingEngine loop not started)")
        resp: Dict[str, Any] = {
            "success": True,
            "message": "Paper bot started (PaperTradingRuntime)",
            "mode": "paper",
            "executor": "PaperTradingRuntime",
        }
        # ROOT-CAUSE FIX: this endpoint used to ONLY set the BotState flag.
        # The V8-D market scan runs in the separate paper worker process
        # (backend/paper/paper_worker.py) — the API never spawned it, so the
        # dashboard showed RUNNING while no scan iteration ever executed.
        # Start now guarantees a live worker, or reports failure honestly.
        from backend.paper import worker_manager as _wm
        if _wm.autospawn_enabled():
            try:
                wait = float(os.environ.get("PAPER_WORKER_START_WAIT", "10"))
            except ValueError:
                wait = 10.0
            spawn = await asyncio.to_thread(_wm.ensure_worker_running, wait)
            if not spawn.get("success"):
                BotState.stop("Paper worker failed to start")
                return {
                    "success": False,
                    "message": "Paper worker failed to start — bot is NOT running. "
                               f"{spawn.get('message') or ''}".strip(),
                    "mode": "paper",
                    "executor": "PaperTradingRuntime",
                    "worker_pid": spawn.get("worker_pid"),
                    "log_path": spawn.get("log_path"),
                    "last_error": spawn.get("last_error"),
                }
            resp["message"] = spawn.get("message") or resp["message"]
            resp["worker_pid"] = spawn.get("worker_pid") or spawn.get("spawned_pid")
            resp["already_running"] = bool(spawn.get("already_running"))
            resp["log_path"] = spawn.get("log_path")
        else:
            resp["worker_autospawn"] = False
            resp["note"] = ("Worker autospawn is disabled (PAPER_WORKER_AUTOSPAWN=0): an external "
                            "paper worker process must be running for scans to execute. "
                            "Check /api/bot/status → runtime_state.")
        rs = paper_runtime_state()
        if rs:
            resp["runtime_state"] = rs.get("state")
            resp["runtime_summary"] = rs.get("summary")
        return resp

    if mode == "live":
        return {
            "success": False,
            "message": "Live trading is not enabled. Refusing START in live mode.",
        }

    # backtest / unknown
    if _engine_ref is not None:
        _engine_ref.start()
    else:
        BotState.start()
    return {"success": True, "message": "Bot started", "mode": mode}


@router.post("/stop")
async def stop_bot() -> Dict[str, Any]:
    """Gracefully stop the trading bot."""
    mode = (_settings_now().mode or "").lower()
    if not BotState.is_running():
        return {"success": False, "message": "Bot is not running"}

    if mode == "paper":
        # Stop only the paper control flag; do not start/stop TradingEngine loop.
        BotState.stop("Manual stop via dashboard (paper)")
        logger.info("Paper STOP — PaperTradingRuntime control flag cleared")
        return {"success": True, "message": "Paper bot stopped", "mode": "paper"}

    if _engine_ref is not None:
        _engine_ref.stop("Manual stop via dashboard")
    else:
        BotState.stop("Manual stop via dashboard")
    return {"success": True, "message": "Bot stopped gracefully"}


@router.post("/kill")
async def emergency_kill() -> Dict[str, Any]:
    """Emergency kill switch — immediately stops all trading."""
    mode = (_settings_now().mode or "").lower()
    paper_rt = _paper_runtime_from_app()
    if paper_rt is not None and hasattr(paper_rt, "kill"):
        try:
            paper_rt.kill.set_level("FULL_SYSTEM_STOP", "dashboard_kill")
        except Exception as exc:
            logger.error("paper kill set failed: %s", type(exc).__name__)

    if _engine_ref is not None:
        _engine_ref.kill("Emergency kill switch activated from dashboard")
    else:
        BotState.kill("Emergency kill switch activated from dashboard")
    return {
        "success": True,
        "message": "EMERGENCY KILL ACTIVATED. All trading stopped immediately.",
        "warning": "You must manually reset the kill switch before trading can resume.",
        "mode": mode,
    }


@router.post("/reset-kill")
async def reset_kill_switch() -> Dict[str, Any]:
    """Reset the kill switch after emergency stop."""
    BotState.reset_kill()
    paper_rt = _paper_runtime_from_app()
    if paper_rt is not None and hasattr(paper_rt, "kill"):
        try:
            paper_rt.kill.set_level("OFF", "dashboard_reset")
        except Exception:
            pass
    if _engine_ref is not None:
        try:
            _engine_ref.risk_manager.deactivate_kill_switch()
        except Exception:
            pass
    return {"success": True, "message": "Kill switch reset. Bot can be started again."}
