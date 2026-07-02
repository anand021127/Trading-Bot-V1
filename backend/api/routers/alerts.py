"""Router for notification status and alert triggers."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from backend.config.settings import load_settings
from backend.notifications.email_alerts import EmailAlerts
from backend.notifications.telegram_alerts import TelegramAlerts

router = APIRouter()
settings = load_settings()


@router.get("/")
async def get_alert_status() -> dict:
    return {
        "email_enabled": settings.notifications.email_enabled,
        "telegram_enabled": settings.notifications.telegram_enabled,
        "smtp_server": settings.notifications.smtp_server,
        "telegram_bot_present": bool(settings.env.get("TELEGRAM_BOT_TOKEN")),
        "telegram_chat_present": bool(settings.env.get("TELEGRAM_CHAT_ID")),
    }


@router.post("/test")
async def send_test_alert(channel: str) -> dict:
    channel = channel.lower()
    if channel == "telegram":
        if not settings.notifications.telegram_enabled:
            raise HTTPException(status_code=400, detail="telegram alerts disabled")
        TelegramAlerts().send_message("Test alert from Upstox trading bot")
        return {"status": "sent", "channel": "telegram"}
    if channel == "email":
        if not settings.notifications.email_enabled:
            raise HTTPException(status_code=400, detail="email alerts disabled")
        EmailAlerts().send_email("Test alert", "This is a test alert from the trading bot.")
        return {"status": "sent", "channel": "email"}
    raise HTTPException(status_code=400, detail="unsupported channel")
