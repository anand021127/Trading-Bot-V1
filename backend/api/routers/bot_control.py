"""Bot control endpoints — start, stop, kill switch, and status."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter

from backend.config.settings import load_settings
from backend.strategy.trading_engine import BotState

router = APIRouter()
settings = load_settings()

# Shared engine reference — set by main.py at startup
_engine_ref: Any = None


def set_engine(engine: Any) -> None:
    global _engine_ref
    _engine_ref = engine


@router.get("/status")
async def bot_status() -> Dict[str, Any]:
    """Full bot status for dashboard."""
    state = BotState.status()
    risk_status: Dict[str, Any] = {}
    if _engine_ref is not None:
        try:
            risk_status = _engine_ref.risk_manager.get_status()
        except Exception:
            pass
    return {
        **state,
        "mode": settings.mode,
        "risk": risk_status,
    }


@router.post("/start")
async def start_bot() -> Dict[str, Any]:
    """Start the trading bot."""
    if BotState.is_running():
        return {"success": False, "message": "Bot is already running"}
    if BotState._kill_switch:
        return {"success": False, "message": "Kill switch is active. Reset it first via /bot/reset-kill"}
    if _engine_ref is not None:
        _engine_ref.start()
    else:
        BotState.start()
    return {"success": True, "message": "Bot started", "mode": settings.mode}


@router.post("/stop")
async def stop_bot() -> Dict[str, Any]:
    """Gracefully stop the trading bot."""
    if not BotState.is_running():
        return {"success": False, "message": "Bot is not running"}
    if _engine_ref is not None:
        _engine_ref.stop("Manual stop via dashboard")
    else:
        BotState.stop("Manual stop via dashboard")
    return {"success": True, "message": "Bot stopped gracefully"}


@router.post("/kill")
async def emergency_kill() -> Dict[str, Any]:
    """Emergency kill switch — immediately stops all trading."""
    if _engine_ref is not None:
        _engine_ref.kill("Emergency kill switch activated from dashboard")
    else:
        BotState.kill("Emergency kill switch activated from dashboard")
    return {
        "success": True,
        "message": "EMERGENCY KILL ACTIVATED. All trading stopped immediately.",
        "warning": "You must manually reset the kill switch before trading can resume.",
    }


@router.post("/reset-kill")
async def reset_kill_switch() -> Dict[str, Any]:
    """Reset the kill switch after emergency stop."""
    BotState.reset_kill()
    if _engine_ref is not None:
        try:
            _engine_ref.risk_manager.deactivate_kill_switch()
        except Exception:
            pass
    return {"success": True, "message": "Kill switch reset. Bot can be started again."}
