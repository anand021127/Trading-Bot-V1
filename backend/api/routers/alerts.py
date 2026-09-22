"""Router for notification status and alert triggers."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

import os

from backend.config.settings import load_settings
from backend.notifications.email_alerts import EmailAlerts
from backend.notifications.telegram_alerts import TelegramAlerts

router = APIRouter()


@router.get("/")
async def get_alert_status() -> dict:
    settings = load_settings()
    return {
        "email_enabled": settings.notifications.email_enabled,
        "telegram_enabled": settings.notifications.telegram_enabled,
        "smtp_server": getattr(settings.notifications, "smtp_server", ""),
        "telegram_bot_present": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
        "telegram_chat_present": bool(os.environ.get("TELEGRAM_CHAT_ID")),
    }


@router.post("/test")
async def send_test_alert(channel: str) -> dict:
    settings = load_settings()
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
