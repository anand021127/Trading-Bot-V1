"""Router for runtime diagnostics and health details."""

from __future__ import annotations

from datetime import datetime
from fastapi import APIRouter

from ...config.settings import load_settings

router = APIRouter()
settings = load_settings()


@router.get("/")
async def diagnostics() -> dict:
    return {
        "status": "ok",
        "mode": settings.mode,
        "timestamp": datetime.now().isoformat(),
        "api_host": settings.api.host,
        "api_port": settings.api.port,
        "broker_base_url": settings.broker.base_url,
        "websocket_url": settings.broker.websocket_url,
    }


@router.get("/env")
async def diagnostics_env() -> dict:
    return {
        "frontend_url": settings.env.get("FRONTEND_URL", ""),
        "upstox_redirect_uri": settings.env.get("UPSTOX_REDIRECT_URI", ""),
        "mode": settings.mode,
    }
