"""FastAPI application — production Upstox trading bot backend."""
from __future__ import annotations

import importlib.util
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Ensure 'backend' is importable when run as `uvicorn backend.api.main:app`
_backend_root = Path(__file__).resolve().parents[2]
if str(_backend_root) not in sys.path:
    sys.path.insert(0, str(_backend_root))

from .routers import (
    alerts_router,
    backtest_router,
    bot_control_router,
    diagnostics_router,
    overview_router,
    performance_router,
    settings_router,
    trading_router,
    websocket_router,
)
from .routers.bot_control import set_engine
from backend.config.settings import load_settings
from backend.database.db_manager import DatabaseManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init DB, build engine, register with bot_control router."""
    s = load_settings()
    db = DatabaseManager(db_path=s.database.path)
    db.init_db()

    # Build engine (does not start trading — user must press Start)
    try:
        from backend.strategy.trading_engine import TradingEngine
        from backend.notifications.telegram_alerts import TelegramAlerts
        from backend.notifications.email_alerts import EmailAlerts
        engine = TradingEngine(
            telegram_alerts=TelegramAlerts() if s.notifications.telegram_enabled else None,
            email_alerts=EmailAlerts() if s.notifications.email_enabled else None,
        )
        set_engine(engine)
        app.state.engine = engine
    except Exception as e:
        print(f"[WARN] Could not build trading engine: {e}")
        app.state.engine = None

    yield
    # Graceful shutdown
    if hasattr(app.state, "engine") and app.state.engine is not None:
        try:
            app.state.engine.stop("Server shutdown")
        except Exception:
            pass


app = FastAPI(
    title="Upstox Trading Bot API",
    version="2.0.0",
    description="Production algorithmic trading backend for NSE NIFTY50 stocks.",
    lifespan=lifespan,
)

settings = load_settings()

# ─── CORS ────────────────────────────────────────────────────────────────────
_frontend_url = os.getenv("FRONTEND_URL", "https://trading-bot-v1-snowy.vercel.app")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        _frontend_url,
        "http://localhost:5173",
        "http://localhost:3000",
    ],
    allow_origin_regex=r"https?://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Routers ──────────────────────────────────────────────────────────────────
app.include_router(overview_router,    prefix="/api")
app.include_router(trading_router,     prefix="/api")
app.include_router(websocket_router,   prefix="/api")
app.include_router(settings_router,    prefix="/api/settings")
app.include_router(diagnostics_router, prefix="/api/diagnostics")
app.include_router(alerts_router,      prefix="/api/alerts")
app.include_router(backtest_router,    prefix="/api/backtest")
app.include_router(performance_router, prefix="/api/performance")
app.include_router(bot_control_router, prefix="/api/bot")


# ─── Core endpoints ────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "mode": settings.mode,
        "timestamp": datetime.now().isoformat(),
        "version": "2.0.0",
    }


@app.get("/api/health")
async def api_health():
    return {"status": "ok"}


@app.get("/")
async def root():
    return {
        "status": "ok",
        "message": "Upstox Trading Bot Backend v2.0",
        "docs": "/docs",
        "health": "/health",
    }


# OAuth token callback
@app.get("/api/settings/token-callback")
async def token_callback_root(code: Optional[str] = None):
    from .routers.settings import token_callback_get
    return await token_callback_get(code)
