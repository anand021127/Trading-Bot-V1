from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from backend.api.routers import (
    alerts_router,
    backtest_router,
    diagnostics_router,
    overview_router,
    performance_router,
    settings_router,
    trading_router,
    websocket_router,
)
from backend.api.routers.websocket import broadcast_price_update
from backend.config.settings import load_settings
from backend.database.db_manager import DatabaseManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    db_manager = DatabaseManager(db_path=settings.database.path)
    db_manager.init_db()

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


app = FastAPI(title="Upstox Trading Bot API", lifespan=lifespan)

settings = load_settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.api.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

app.include_router(overview_router, prefix="/api")
app.include_router(trading_router, prefix="/api/trading")
app.include_router(websocket_router, prefix="/api")
app.include_router(settings_router, prefix="/api/settings")
app.include_router(diagnostics_router, prefix="/api/diagnostics")
app.include_router(alerts_router, prefix="/api/alerts")
app.include_router(backtest_router, prefix="/api/backtest")
app.include_router(performance_router, prefix="/api/performance")


@app.get("/health")
async def health():
    return {"status": "ok", "mode": settings.mode, "timestamp": datetime.now().isoformat()}


@app.get("/api/health")
async def api_health():
    return {"status": "ok"}


@app.get("/")
async def root():
    return {"status": "ok", "message": "Upstox trading bot backend"}


@app.post("/api/settings/token-callback")
async def token_callback(code: Optional[str] = Form(None)):
    # Minimal stub: in the real app this would exchange the code for tokens
    if not code:
        return JSONResponse({"detail": "missing code"}, status_code=400)
    return {"status": "received", "code": code}
