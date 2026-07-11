"""Settings router — persistent configuration via SQLite + YAML fallback."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from fastapi import APIRouter, HTTPException

from backend.config.settings import load_settings
from backend.database.db_manager import DatabaseManager

router = APIRouter()
settings = load_settings()

SETTINGS_PATH = Path(__file__).resolve().parents[3] / "backend" / "config" / "settings.yaml"
_db = DatabaseManager(db_path=settings.database.path)


def _yaml_defaults() -> Dict[str, Any]:
    """Load defaults from settings.yaml (always available)."""
    return {
        "mode": settings.mode,
        "capital": {
            "total": settings.capital.total,
            "max_allocation_per_trade": settings.capital.max_allocation_per_trade,
            "cash_buffer": settings.capital.cash_buffer,
        },
        "risk": {
            "max_risk_per_trade_pct":   settings.risk.max_risk_per_trade_pct,
            "max_daily_loss_pct":       settings.risk.max_daily_loss_pct,
            "max_trades_per_day":       settings.risk.max_trades_per_day,
            "max_concurrent_positions": settings.risk.max_concurrent_positions,
            "max_consecutive_losses":   settings.risk.max_consecutive_losses,
        },
        "strategy": {
            "orb_window_start":   getattr(settings.strategy, "orb_window_start",   "09:15"),
            "orb_window_end":     getattr(settings.strategy, "orb_window_end",     "09:30"),
            "entry_window_start": getattr(settings.strategy, "entry_window_start", "09:30"),
            "entry_window_end":   getattr(settings.strategy, "entry_window_end",   "12:30"),
            "exit_all_by":        getattr(settings.strategy, "exit_all_by",        "14:45"),
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
            "email_enabled":    getattr(settings.notifications, "email_enabled",    False),
            "telegram_enabled": getattr(settings.notifications, "telegram_enabled", False),
            "sender_email":     bool(os.getenv("SENDER_EMAIL") or os.getenv("NOTIFICATION_EMAIL") or getattr(settings.notifications, "sender_email", "")),
            "recipient_email":  bool(os.getenv("RECIPIENT_EMAIL") or os.getenv("NOTIFICATION_EMAIL") or getattr(settings.notifications, "recipient_email", "")),
        },
    }


@router.get("/")
async def get_settings() -> Dict[str, Any]:
    """
    Return settings. Priority: DB (persistent) > YAML defaults.
    This means saved settings survive Render restarts.
    """
    try:
        blob = _db.load_settings_blob()
        if blob:
            return blob
    except Exception:
        pass
    return _yaml_defaults()


@router.put("/")
async def update_settings(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Persist settings to SQLite DB (survives restarts) AND settings.yaml (best-effort).
    """
    try:
        # Load existing saved settings (or defaults)
        current: Dict[str, Any] = {}
        try:
            current = _db.load_settings_blob() or _yaml_defaults()
        except Exception:
            current = _yaml_defaults()

        # Deep-merge only known keys
        for key in ("mode", "capital", "risk", "strategy", "indicators", "notifications", "universe"):
            if key in body:
                if isinstance(body[key], dict) and isinstance(current.get(key), dict):
                    current[key].update(body[key])
                else:
                    current[key] = body[key]

        # Save to SQLite (primary persistent storage)
        _db.save_settings_blob(current)

        # Also try to update settings.yaml (best-effort, may be read-only on Render)
        try:
            if SETTINGS_PATH.exists():
                with open(SETTINGS_PATH) as f:
                    yaml_current = yaml.safe_load(f) or {}
                for key in ("mode", "capital", "risk", "strategy", "indicators", "notifications"):
                    if key in current:
                        if isinstance(current[key], dict) and isinstance(yaml_current.get(key), dict):
                            yaml_current[key].update(current[key])
                        else:
                            yaml_current[key] = current[key]
                with open(SETTINGS_PATH, "w") as f:
                    yaml.dump(yaml_current, f, default_flow_style=False, allow_unicode=True)
        except Exception:
            pass  # Read-only filesystem on Render is fine — DB is the source of truth

        return {"saved": True, "restart_required": False, "storage": "database"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/env-status")
async def get_env_status() -> Dict[str, bool]:
    """Return which env vars are set (never expose values)."""
    # Also check DB-stored token
    db_token = ""
    try:
        db_token = _db.load_token()
    except Exception:
        pass
    return {
        "UPSTOX_CLIENT_ID":     bool(os.getenv("UPSTOX_CLIENT_ID")),
        "UPSTOX_CLIENT_SECRET": bool(os.getenv("UPSTOX_CLIENT_SECRET")),
        "UPSTOX_ACCESS_TOKEN":  bool(os.getenv("UPSTOX_ACCESS_TOKEN") or db_token),
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
    redirect_uri = os.getenv("UPSTOX_REDIRECT_URI", "")
    if not redirect_uri:
        # Build redirect URI from Render URL
        render_url = os.getenv("RENDER_EXTERNAL_URL", "")
        redirect_uri = f"{render_url}/api/settings/token-callback" if render_url else "http://localhost:8000/api/settings/token-callback"

    if not client_id:
        raise HTTPException(status_code=400, detail="UPSTOX_CLIENT_ID not set in Render environment variables.")

    auth_url = (
        "https://api.upstox.com/v2/login/authorization/dialog"
        f"?response_type=code&client_id={client_id}&redirect_uri={redirect_uri}"
    )
    return {"auth_url": auth_url, "redirect_uri": redirect_uri}


async def token_callback_get(code: Optional[str] = None) -> Dict[str, Any]:
    """Handle OAuth redirect — exchange code, save token to DB and env."""
    if not code:
        return {"status": "error", "detail": "No code in redirect. Try generating token again."}
    try:
        import httpx
        client_id     = os.getenv("UPSTOX_CLIENT_ID", "")
        client_secret = os.getenv("UPSTOX_CLIENT_SECRET", "")
        render_url    = os.getenv("RENDER_EXTERNAL_URL", "")
        redirect_uri  = os.getenv("UPSTOX_REDIRECT_URI", "")
        if not redirect_uri and render_url:
            redirect_uri = f"{render_url}/api/settings/token-callback"

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
            # Save to environment (current process)
            os.environ["UPSTOX_ACCESS_TOKEN"] = token
            # Save to SQLite (survives restarts within same Render disk)
            try:
                _db.save_token(token)
            except Exception:
                pass
            return {
                "status": "success",
                "message": "Token saved to database and environment. Bot is ready to trade.",
            }
        return {"status": "error", "detail": data.get("message", "Token exchange failed")}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@router.get("/broker-status")
async def get_broker_status() -> Dict[str, Any]:
    """
    Real broker connection status check.
    Does NOT just check if token exists — actually calls Upstox API.
    """
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "")
    if not token:
        try:
            token = _db.load_token()
            if token:
                os.environ["UPSTOX_ACCESS_TOKEN"] = token
        except Exception:
            pass

    status = {
        "token_present": bool(token),
        "token_valid": False,
        "api_reachable": False,
        "websocket_url": settings.broker.websocket_url,
        "overall": "DISCONNECTED",
        "reason": "",
    }

    if not token:
        status["reason"] = "No access token. Go to Settings → Generate Token."
        return status

    try:
        from backend.broker.upstox_client import UpstoxClient
        client = UpstoxClient(access_token=token)
        valid = client.is_token_valid()
        status["token_valid"] = valid
        status["api_reachable"] = True

        if valid:
            status["overall"] = "CONNECTED"
            status["reason"] = "Token valid and Upstox API reachable."
        else:
            status["overall"] = "DISCONNECTED"
            status["reason"] = "Token present but rejected by Upstox (401). Regenerate token."
    except Exception as e:
        err = str(e)
        status["api_reachable"] = False
        status["reason"] = f"Upstox API error: {err}"
        if "401" in err:
            status["reason"] = "Token expired. Go to Settings → Generate Token."

    return status
