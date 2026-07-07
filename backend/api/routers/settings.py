"""Settings router — configuration management and token handling."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from fastapi import APIRouter, HTTPException

from backend.config.settings import load_settings

router = APIRouter()
settings = load_settings()

SETTINGS_PATH = Path(__file__).resolve().parents[3] / "backend" / "config" / "settings.yaml"


@router.get("/")
async def get_settings() -> Dict[str, Any]:
    """Return all settings (never expose secrets)."""
    notif = settings.notifications
    return {
        "mode": settings.mode,
        "capital": {
            "total": settings.capital.total,
            "max_allocation_per_trade": settings.capital.max_allocation_per_trade,
            "cash_buffer": settings.capital.cash_buffer,
        },
        "risk": {
            "max_risk_per_trade_pct":    settings.risk.max_risk_per_trade_pct,
            "max_daily_loss_pct":        settings.risk.max_daily_loss_pct,
            "max_trades_per_day":        settings.risk.max_trades_per_day,
            "max_concurrent_positions":  settings.risk.max_concurrent_positions,
            "max_consecutive_losses":    settings.risk.max_consecutive_losses,
        },
        "strategy": {
            "orb_window_start":   getattr(settings.strategy, "orb_window_start",  "09:15"),
            "orb_window_end":     getattr(settings.strategy, "orb_window_end",    "09:30"),
            "entry_window_start": getattr(settings.strategy, "entry_window_start","09:30"),
            "entry_window_end":   getattr(settings.strategy, "entry_window_end",  "12:30"),
            "exit_all_by":        getattr(settings.strategy, "exit_all_by",       "14:45"),
        },
        "indicators": {
            "ema_fast":          getattr(settings.indicators, "ema_fast",          20),
            "ema_slow":          getattr(settings.indicators, "ema_slow",          50),
            "ema_trend":         getattr(settings.indicators, "ema_trend",         200),
            "rsi_period":        getattr(settings.indicators, "rsi_period",        14),
            "rsi_min":           getattr(settings.indicators, "rsi_min",           55),
            "rsi_max":           getattr(settings.indicators, "rsi_max",           75),
            "atr_period":        getattr(settings.indicators, "atr_period",        14),
            "choppiness_max":    getattr(settings.indicators, "choppiness_max",    61.8),
            "volume_multiplier": getattr(settings.indicators, "volume_multiplier", 1.5),
        },
        "notifications": {
            "email_enabled":    getattr(notif, "email_enabled",    False),
            "telegram_enabled": getattr(notif, "telegram_enabled", False),
            # Show whether email addresses are configured (from env or yaml)
            "sender_email":    bool(
                os.getenv("SENDER_EMAIL") or
                os.getenv("NOTIFICATION_EMAIL") or
                getattr(notif, "sender_email", "")
            ),
            "recipient_email": bool(
                os.getenv("RECIPIENT_EMAIL") or
                os.getenv("NOTIFICATION_EMAIL") or
                getattr(notif, "recipient_email", "")
            ),
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
    return {
        "UPSTOX_CLIENT_ID":     bool(os.getenv("UPSTOX_CLIENT_ID")),
        "UPSTOX_CLIENT_SECRET": bool(os.getenv("UPSTOX_CLIENT_SECRET")),
        "UPSTOX_ACCESS_TOKEN":  bool(os.getenv("UPSTOX_ACCESS_TOKEN")),
        "EMAIL_PASSWORD":       bool(os.getenv("EMAIL_PASSWORD")),
        "SENDER_EMAIL":         bool(os.getenv("SENDER_EMAIL") or os.getenv("NOTIFICATION_EMAIL")),
        "RECIPIENT_EMAIL":      bool(os.getenv("RECIPIENT_EMAIL") or os.getenv("NOTIFICATION_EMAIL")),
        "TELEGRAM_BOT_TOKEN":   bool(os.getenv("TELEGRAM_BOT_TOKEN")),
        "TELEGRAM_CHAT_ID":     bool(os.getenv("TELEGRAM_CHAT_ID")),
    }


@router.post("/regenerate-token")
async def regenerate_token() -> Dict[str, str]:
    """Return Upstox OAuth authorization URL."""
    client_id    = os.getenv("UPSTOX_CLIENT_ID", "")
    redirect_uri = os.getenv("UPSTOX_REDIRECT_URI", "http://localhost:8080/callback")
    if not client_id:
        raise HTTPException(status_code=400, detail="UPSTOX_CLIENT_ID not set in environment variables.")
    auth_url = (
        "https://api.upstox.com/v2/login/authorization/dialog"
        f"?response_type=code&client_id={client_id}&redirect_uri={redirect_uri}"
    )
    return {"auth_url": auth_url}


async def token_callback_get(code: Optional[str] = None) -> Dict[str, Any]:
    """Handle OAuth redirect — exchange code for access token."""
    if not code:
        return {"status": "error", "detail": "No code in redirect. Try generating token again."}
    try:
        import httpx
        client_id     = os.getenv("UPSTOX_CLIENT_ID", "")
        client_secret = os.getenv("UPSTOX_CLIENT_SECRET", "")
        redirect_uri  = os.getenv("UPSTOX_REDIRECT_URI", "http://localhost:8080/callback")
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
            os.environ["UPSTOX_ACCESS_TOKEN"] = token
            return {"status": "success", "message": "Token saved. Bot is ready to trade."}
        return {"status": "error", "detail": data.get("message", "Token exchange failed")}
    except Exception as e:
        return {"status": "error", "detail": str(e)}
