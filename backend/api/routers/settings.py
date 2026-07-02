"""Router for application settings and auth-related configuration."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...config.settings import load_settings

router = APIRouter()
settings = load_settings()


class AppSettingsResponse(BaseModel):
    mode: str
    api_host: str
    api_port: int
    broker_base_url: str
    websocket_url: str
    frontend_url: str
    notifications: dict


@router.get("/", response_model=AppSettingsResponse)
async def get_settings() -> AppSettingsResponse:
    return AppSettingsResponse(
        mode=settings.mode,
        api_host=settings.api.host,
        api_port=settings.api.port,
        broker_base_url=settings.broker.base_url,
        websocket_url=settings.broker.websocket_url,
        frontend_url=settings.env.get("FRONTEND_URL", ""),
        notifications={
            "email_enabled": settings.notifications.email_enabled,
            "telegram_enabled": settings.notifications.telegram_enabled,
        },
    )


@router.post("/token-callback")
async def token_callback(code: str) -> dict:
    if not code:
        raise HTTPException(status_code=400, detail="missing code")
    return {"status": "received", "code": code}
