"""Router for application settings, env status, and token management."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.config.settings import load_settings

router = APIRouter()
settings = load_settings()

SETTINGS_PATH = Path(__file__).resolve().parents[3] / "backend" / "config" / "settings.yaml"


@router.get("/")
async def get_settings() -> Dict[str, Any]:
    """Return all settings (without secrets)."""
    return {
        "mode": settings.mode,
        "api_host": settings.api.host,
        "api_port": settings.api.port,
        "broker_base_url": settings.broker.base_url,
        "websocket_url": settings.broker.websocket_url,
        "capital": {
            "total": settings.capital.total,
            "max_allocation_per_trade": settings.capital.max_allocation_per_trade,
            "cash_buffer": settings.capital.cash_buffer,
        },
        "risk": {
            "max_risk_per_trade_pct": settings.risk.max_risk_per_trade_pct,
            "max_daily_loss_pct": settings.risk.max_daily_loss_pct,
            "max_trades_per_day": settings.risk.max_trades_per_day,
            "max_concurrent_positions": settings.risk.max_concurrent_positions,
            "max_consecutive_losses": settings.risk.max_consecutive_losses,
        },
        "strategy": {
            "orb_window_start": settings.strategy.orb_window_start,
            "orb_window_end": settings.strategy.orb_window_end,
            "entry_window_start": settings.strategy.entry_window_start,
            "entry_window_end": settings.strategy.entry_window_end,
            "exit_all_by": settings.strategy.exit_all_by,
        },
        "indicators": {
            "ema_fast": settings.indicators.ema_fast,
            "ema_slow": settings.indicators.ema_slow,
            "ema_trend": settings.indicators.ema_trend,
            "rsi_period": settings.indicators.rsi_period,
            "rsi_min": settings.indicators.rsi_min,
            "rsi_max": settings.indicators.rsi_max,
            "atr_period": settings.indicators.atr_period,
            "choppiness_max": settings.indicators.choppiness_max,
            "volume_multiplier": settings.indicators.volume_multiplier,
        },
        "notifications": {
            "email_enabled": settings.notifications.email_enabled,
            "telegram_enabled": settings.notifications.telegram_enabled,
            "sender_email": settings.notifications.sender_email,
            "recipient_email": settings.notifications.recipient_email,
        },
    }


@router.put("/")
async def update_settings(body: Dict[str, Any]) -> Dict[str, Any]:
    """Persist updated settings to settings.yaml."""
    try:
        if SETTINGS_PATH.exists():
            with open(SETTINGS_PATH) as f:
                current = yaml.safe_load(f) or {}
        else:
            current = {}

        # Deep-merge only known top-level keys
        for key in ("mode", "capital", "risk", "strategy", "indicators", "notifications"):
            if key in body:
                if isinstance(body[key], dict) and isinstance(current.get(key), dict):
                    current[key].update(body[key])
                else:
                    current[key] = body[key]

        with open(SETTINGS_PATH, "w") as f:
            yaml.dump(current, f, default_flow_style=False, allow_unicode=True)

        return {"saved": True, "restart_required": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/env-status")
async def get_env_status() -> Dict[str, bool]:
    """Return which environment variables are set (never their values)."""
    keys = [
        "UPSTOX_CLIENT_ID",
        "UPSTOX_CLIENT_SECRET",
        "UPSTOX_ACCESS_TOKEN",
        "EMAIL_PASSWORD",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ]
    return {k: bool(os.getenv(k)) for k in keys}


@router.post("/regenerate-token")
async def regenerate_token() -> Dict[str, str]:
    """Return the Upstox OAuth authorization URL for the user to open."""
    client_id = os.getenv("UPSTOX_CLIENT_ID", "")
    redirect_uri = os.getenv("UPSTOX_REDIRECT_URI", "http://localhost:8080/callback")
    if not client_id:
        raise HTTPException(status_code=400, detail="UPSTOX_CLIENT_ID not set")
    auth_url = (
        f"https://api.upstox.com/v2/login/authorization/dialog"
        f"?response_type=code&client_id={client_id}&redirect_uri={redirect_uri}"
    )
    return {"auth_url": auth_url}


@router.get("/token-callback")
async def token_callback_get(code: Optional[str] = None) -> Dict[str, Any]:
    """Handle OAuth redirect — exchange code for access token."""
    if not code:
        return {"status": "error", "detail": "No code provided"}
    try:
        import httpx
        client_id = os.getenv("UPSTOX_CLIENT_ID", "")
        client_secret = os.getenv("UPSTOX_CLIENT_SECRET", "")
        redirect_uri = os.getenv("UPSTOX_REDIRECT_URI", "http://localhost:8080/callback")
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.upstox.com/v2/login/authorization/token",
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        data = r.json()
        token = data.get("access_token", "")
        if token:
            # In production on Render, this sets the env var for the current process
            os.environ["UPSTOX_ACCESS_TOKEN"] = token
            return {"status": "success", "message": "Token saved. Bot is ready to trade."}
        return {"status": "error", "detail": data.get("message", "Token exchange failed")}
    except Exception as e:
        return {"status": "error", "detail": str(e)}
