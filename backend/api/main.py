"""FastAPI application entry point."""
import importlib.util
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# Ensure 'backend' package is importable when run as uvicorn backend.api.main:app
service_root = Path(__file__).resolve().parents[1]
if importlib.util.find_spec("backend") is None:
    backend_pkg = ModuleType("backend")
    backend_pkg.__path__ = [str(service_root)]
    sys.modules["backend"] = backend_pkg

from .routers import (
    alerts_router,
    backtest_router,
    diagnostics_router,
    overview_router,
    performance_router,
    settings_router,
    trading_router,
    websocket_router,
)
from .routers.websocket import broadcast_price_update
from backend.config.settings import load_settings
from backend.database.db_manager import DatabaseManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = load_settings()
    db = DatabaseManager(db_path=s.database.path)
    db.init_db()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        broadcast_price_update,
        IntervalTrigger(seconds=5),
        id="ws_price_broadcast",
    )
    scheduler.start()
    app.state.scheduler = scheduler
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(
    title="Upstox Trading Bot API",
    version="1.0.0",
    lifespan=lifespan,
)

settings = load_settings()

# CORS — allow Vercel frontend + localhost dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.api.cors_origins,
    allow_origin_regex=r"https?://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ──────────────────────────────────────────────────────────────────
# /api/overview
app.include_router(overview_router, prefix="/api")

# /api/trades, /api/positions, /api/prices, /api/paper
app.include_router(trading_router, prefix="/api")

# /api/ws (WebSocket)
app.include_router(websocket_router, prefix="/api")

# /api/settings
app.include_router(settings_router, prefix="/api/settings")

# /api/diagnostics
app.include_router(diagnostics_router, prefix="/api/diagnostics")

# /api/alerts
app.include_router(alerts_router, prefix="/api/alerts")

# /api/backtest
app.include_router(backtest_router, prefix="/api/backtest")

# /api/performance
app.include_router(performance_router, prefix="/api/performance")


# ── Core endpoints ────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "mode": settings.mode,
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/health")
async def api_health():
    return {"status": "ok"}


@app.get("/")
async def root():
    return {
        "status": "ok",
        "message": "Upstox Trading Bot Backend",
        "docs": "/docs",
        "health": "/health",
    }


# OAuth callback (GET — Upstox redirects here with ?code=...)
@app.get("/api/settings/token-callback")
async def token_callback_root(code: Optional[str] = None):
    from .routers.settings import token_callback_get
    return await token_callback_get(code)
