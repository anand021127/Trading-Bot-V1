"""Bot control endpoints — start, stop, kill switch, and status."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter

from backend.config.settings import load_settings
from backend.strategy.trading_engine import BotState

router = APIRouter()
settings = load_settings()
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
        return getattr(getattr(main_mod, "app", None), "state", None) and getattr(
            main_mod.app.state, "paper_runtime", None
        )
    except Exception:
        return None


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
    return {
        **state,
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
    mode = (settings.mode or "").lower()
    if BotState.is_running():
        return {"success": False, "message": "Bot is already running"}
    if BotState.status()["kill_switch_active"]:
        return {"success": False, "message": "Kill switch is active. Reset it first via /bot/reset-kill"}

    if mode == "paper":
        paper_rt = _paper_runtime_from_app()
        if paper_rt is None:
            return {
                "success": False,
                "message": "PaperTradingRuntime not attached. Refusing to start TradingEngine in paper mode.",
            }
        # Paper runtime is already constructed at lifespan; mark BotState running only.
        # Do NOT call TradingEngine.start()/run_forever in paper mode.
        BotState.start()
        logger.info("Paper START — PaperTradingRuntime only (TradingEngine loop not started)")
        return {
            "success": True,
            "message": "Paper bot started (PaperTradingRuntime)",
            "mode": "paper",
            "executor": "PaperTradingRuntime",
        }

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
    mode = (settings.mode or "").lower()
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
    mode = (settings.mode or "").lower()
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
