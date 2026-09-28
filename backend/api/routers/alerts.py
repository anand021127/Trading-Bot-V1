"""Router for notification status and alert triggers."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

import os

from backend.config.settings import load_settings
from backend.notifications.telegram_alerts import TelegramAlerts

router = APIRouter()


@router.get("/")
async def get_alert_status() -> dict:
    settings = load_settings()
    return {
        "telegram_enabled": settings.notifications.telegram_enabled,
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
    raise HTTPException(status_code=400, detail="unsupported channel")
