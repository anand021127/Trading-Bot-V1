"""FastAPI application — production Upstox trading bot backend."""
from __future__ import annotations

import asyncio
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
    paper_router,
    performance_router,
    scanner_router,
    settings_router,
    strategy_router,
    trading_router,
    universe_router,
    websocket_router,
)
from .routers.bot_control import set_engine
from .routers.scanner import set_scanner
from .routers.strategy import set_engine as set_strategy_engine
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
        set_strategy_engine(engine)
        app.state.engine = engine
    except Exception as e:
        print(f"[WARN] Could not build trading engine: {e}")
        app.state.engine = None

    # Start the real Upstox v3 market-data WebSocket (no mock prices).
    # If there's no token yet, this stays in 'auth_failed' status and the
    # frontend must show that honestly rather than fabricate ticks.
    app.state.ws_client = None
    try:
        from backend.broker.websocket_client import UpstoxWebSocketClient
        from backend.broker.upstox_client import ALL_INSTRUMENTS
        from backend.api.websocket import update_price_cache

        token = os.getenv("UPSTOX_ACCESS_TOKEN", "")
        if not token:
            try:
                token = db.load_token()
            except Exception:
                token = ""

        ws_client = UpstoxWebSocketClient(
            access_token=token,
            instrument_keys=list(ALL_INSTRUMENTS.values()),
            on_price_update=update_price_cache,
            mode="full",
        )
        ws_client.start()
        app.state.ws_client = ws_client
    except Exception as e:
        print(f"[WARN] Could not start Upstox v3 WebSocket client: {e}")

    # Live scanner — runs continuously regardless of BotState (order
    # execution), so the dashboard always shows what's being analyzed.
    app.state.scanner = None
    try:
        from backend.scanner.live_scanner import LiveScanner
        from backend.config.universe_config import load_universe_config

        def _resolve_universe() -> list:
            try:
                return load_universe_config(db).resolve_symbols()
            except Exception:
                return []

        def _resolve_universe_mode() -> str:
            try:
                return load_universe_config(db).mode
            except Exception:
                return "STOCKS"

        if app.state.engine is not None:
            scanner = LiveScanner(
                trading_engine=app.state.engine,
                universe_resolver=_resolve_universe,
                mode_resolver=_resolve_universe_mode,
                seconds_between_symbols=3.0,
            )
            scanner.start()
            set_scanner(scanner)
            app.state.scanner = scanner
    except Exception as e:
        print(f"[WARN] Could not start live scanner: {e}")

    # Trading loop — runs in-process as a background task, same pattern as
    # the scanner above. This used to require a SEPARATE Render worker
    # service (backend/worker.py) running as its own OS process. On
    # Render's free tier that meant paying for two spin-down-prone
    # services instead of one, AND it was the root cause of a real bug:
    # BotState lived independently in each process's memory, so
    # Start/Stop/Kill on the dashboard (this process) had no effect on
    # whether the OTHER process actually traded. Running it here instead
    # means there is only ever one process, one BotState, one truth — the
    # DB-backed BotState from the previous fix is kept as a safety net
    # (still lets a genuinely separate worker.py be run manually if
    # someone wants to split load later), but nothing requires it anymore.
    #
    # backend/worker.py is left in place and still works standalone if you
    # explicitly want a separate process — it is simply no longer required
    # for the bot to function correctly on a single Render service.
    app.state.trading_task = None
    try:
        if app.state.engine is not None and settings.mode in ("paper", "live"):
            app.state.trading_task = asyncio.create_task(app.state.engine.run_forever())
    except Exception as e:
        print(f"[WARN] Could not start in-process trading loop: {e}")

    yield
    # Graceful shutdown
    if getattr(app.state, "trading_task", None) is not None:
        try:
            app.state.trading_task.cancel()
        except Exception:
            pass
    if getattr(app.state, "scanner", None) is not None:
        try:
            app.state.scanner.stop()
        except Exception:
            pass
    if getattr(app.state, "ws_client", None) is not None:
        try:
            app.state.ws_client.stop()
        except Exception:
            pass
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
app.include_router(paper_router,       prefix="/api/paper")
app.include_router(bot_control_router, prefix="/api/bot")
app.include_router(strategy_router,    prefix="/api/strategy")
app.include_router(universe_router,    prefix="/api/universe")
app.include_router(scanner_router,     prefix="/api/scanner")


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


@app.get("/api/version")
async def api_version():
    """Lets you verify what's actually deployed vs. what you think you
    deployed — if any of these are missing/false on your live URL, the
    backend hasn't picked up the latest code (stale build, wrong branch,
    or requirements.txt not re-installed)."""
    sdk_installed = True
    try:
        import upstox_client  # noqa: F401
    except ImportError:
        sdk_installed = False

    try:
        routes_present = set(app.openapi().get("paths", {}).keys())
    except Exception:
        routes_present = set()

    from backend.broker.instrument_master import get_master_status

    return {
        "backend_build": "v14-dynamic-instrument-master",
        "features": {
            "upstox_v3_websocket": True,
            "upstox_python_sdk_installed": sdk_installed,
            "multi_strategy_engine": True,
            "live_scanner": True,
            "universe_selection": True,
            "realistic_backtest_engine": True,
            "orb_daily_reset_fix": True,
            "trailing_stop_manager": True,
            "index_prices_endpoint": True,
            "dynamic_instrument_master": True,
            "professional_options_risk_controls": True,
        },
        "scanner_router_registered": "/api/scanner/status" in routes_present,
        "universe_router_registered": "/api/universe/" in routes_present,
        "backtest_v9_registered": "/api/backtest/run" in routes_present,
        "ws_client_active": getattr(app.state, "ws_client", None) is not None,
        "scanner_active": getattr(app.state, "scanner", None) is not None,
        "engine_active": getattr(app.state, "engine", None) is not None,
        "instrument_master": get_master_status(),
    }


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
