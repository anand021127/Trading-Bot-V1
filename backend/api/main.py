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
    options_router,
    paper_router,
    performance_router,
    scanner_router,
    settings_router,
    strategy_router,
    trading_router,
    universe_router,
    upstox_v3_auth_router,
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

    # ── Health monitoring ──────────────────────────────────────────────
    from backend.health.health_monitor import health_monitor, ComponentStatus
    from backend.health.task_supervisor import TaskSupervisor

    health_monitor.register("scanner")
    health_monitor.register("websocket")
    health_monitor.register("trading_engine")
    health_monitor.register("database")
    health_monitor.update_status("database", ComponentStatus.RUNNING)
    health_monitor.log_event("bot", "BOT_STARTED", "Backend process starting")

    supervisor = TaskSupervisor(check_interval=10)
    app.state.supervisor = supervisor
    app.state.health_monitor = health_monitor

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
        health_monitor.update_status("trading_engine", ComponentStatus.RUNNING)
    except Exception as e:
        print(f"[WARN] Could not build trading engine: {e}")
        app.state.engine = None
        health_monitor.update_status("trading_engine", ComponentStatus.FAILED)
        health_monitor.record_error("trading_engine", str(e))

    # Start the real Upstox v3 market-data WebSocket (no mock prices).
    # If there's no token yet, this stays in 'auth_failed' status and the
    # frontend must show that honestly rather than fabricate ticks.
    app.state.ws_client = None
    try:
        from backend.broker.websocket_client import UpstoxWebSocketClient
        from backend.broker.upstox_client import ALL_INSTRUMENTS
        from backend.api.websocket import update_price_cache

        from backend.broker.token_resolver import resolve_upstox_token
        token = resolve_upstox_token()

        ws_client = UpstoxWebSocketClient(
            access_token=token,
            instrument_keys=list(ALL_INSTRUMENTS.values()),
            on_price_update=update_price_cache,
            mode="full",
        )
        ws_client.start()
        app.state.ws_client = ws_client
        health_monitor.update_status("websocket", ComponentStatus.STARTING)
    except Exception as e:
        print(f"[WARN] Could not start Upstox v3 WebSocket client: {e}")
        health_monitor.update_status("websocket", ComponentStatus.FAILED)
        health_monitor.record_error("websocket", str(e))

    # Live scanner — runs continuously regardless of BotState (order
    # execution), so the dashboard always shows what's being analyzed.
    # Now managed by the TaskSupervisor for automatic recovery.
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
                return "OPTIONS"

        if app.state.engine is not None:
            scanner = LiveScanner(
                trading_engine=app.state.engine,
                universe_resolver=_resolve_universe,
                mode_resolver=_resolve_universe_mode,
                seconds_between_symbols=3.0,
            )
            set_scanner(scanner)
            app.state.scanner = scanner

            # Register with supervisor instead of calling scanner.start()
            # directly — the supervisor will start it AND auto-restart
            # if it dies unexpectedly.
            supervisor.register(
                "scanner",
                factory=scanner.run_forever,
                max_restarts=10,
            )
    except Exception as e:
        print(f"[WARN] Could not start live scanner: {e}")
        health_monitor.record_error("scanner", str(e))

    # ── Heartbeat background task ──────────────────────────────────────
    async def _heartbeat_loop() -> None:
        """Updates the global heartbeat every 5 seconds. This proves the
        event loop is alive and responsive."""
        while True:
            health_monitor.global_heartbeat()
            health_monitor.heartbeat("bot_process")
            await asyncio.sleep(5)

    health_monitor.register("bot_process")
    health_monitor.update_status("bot_process", ComponentStatus.RUNNING)
    supervisor.register("heartbeat", factory=_heartbeat_loop, max_restarts=100)

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
        if app.state.engine is not None and s.mode in ("paper", "live"):
            app.state.trading_task = asyncio.create_task(app.state.engine.run_forever())
    except Exception as e:
        print(f"[WARN] Could not start in-process trading loop: {e}")

    # Start the supervisor (which starts all registered tasks)
    supervisor.start()
    health_monitor.log_event("bot", "BOT_STARTED", "All components initialized, supervisor started")

    yield
    # Graceful shutdown
    health_monitor.log_event("bot", "BOT_STOPPED", "Backend shutting down")
    supervisor.stop()
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
    description="Production algorithmic trading backend for index options.",
    lifespan=lifespan,
)

settings = load_settings()

# ─── CORS ────────────────────────────────────────────────────────────────────
# Explicit allowed origins for production Vercel frontend, local dev, and environment overrides
ALLOWED_ORIGINS = [
    # Production Vercel domains
    "https://trading-bot-v1-egi204u8k-anand0211277s-projects.vercel.app",
    "https://trading-bot-v1-snowy.vercel.app",
    "https://trading-bot-v1.vercel.app",
    # Local development URLs
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
]

_frontend_url_env = os.getenv("FRONTEND_URL")
if _frontend_url_env:
    for origin in _frontend_url_env.split(","):
        origin_clean = origin.strip().rstrip("/")
        if origin_clean and origin_clean not in ALLOWED_ORIGINS:
            ALLOWED_ORIGINS.append(origin_clean)

_cors_origins_env = os.getenv("CORS_ORIGINS")
if _cors_origins_env:
    for origin in _cors_origins_env.split(","):
        origin_clean = origin.strip().rstrip("/")
        if origin_clean and origin_clean not in ALLOWED_ORIGINS:
            ALLOWED_ORIGINS.append(origin_clean)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=r"^https://trading-bot-v1.*\.vercel\.app$",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Accept",
        "Origin",
        "User-Agent",
        "DNT",
        "Cache-Control",
        "X-Mx-ReqToken",
        "Keep-Alive",
        "X-Requested-With",
        "If-Modified-Since",
        "X-CSRF-Token",
        "Range",
        "*",
    ],
    expose_headers=[
        "Content-Length",
        "Content-Range",
        "Content-Disposition",
        "X-Request-ID",
    ],
    max_age=86400,
)

# ─── Routers ──────────────────────────────────────────────────────────────────
app.include_router(overview_router,       prefix="/api")
app.include_router(options_router,        prefix="/api/options")
app.include_router(trading_router,        prefix="/api")
app.include_router(websocket_router,      prefix="/api")
app.include_router(settings_router,       prefix="/api/settings")
app.include_router(upstox_v3_auth_router, prefix="")
app.include_router(diagnostics_router,    prefix="/api/diagnostics")
app.include_router(alerts_router,         prefix="/api/alerts")
app.include_router(backtest_router,       prefix="/api/backtest")
app.include_router(performance_router,    prefix="/api/performance")
app.include_router(paper_router,          prefix="/api/paper")
app.include_router(bot_control_router,    prefix="/api/bot")
app.include_router(strategy_router,       prefix="/api/strategy")
app.include_router(universe_router,       prefix="/api/universe")
app.include_router(scanner_router,        prefix="/api/scanner")


# ─── Core endpoints ────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    """Lightweight health endpoint — reads in-memory state only, never
    performs expensive broker/API operations."""
    try:
        from backend.health.health_monitor import health_monitor
        snapshot = health_monitor.snapshot()
        return {
            "status": "ok",
            "mode": settings.mode,
            "timestamp": datetime.now().isoformat(),
            "version": "2.0.0",
            "bot_status": snapshot.get("bot_status", "UNKNOWN"),
            "uptime_seconds": snapshot.get("uptime_seconds", 0),
            "started_at": snapshot.get("started_at"),
            "process_id": snapshot.get("process_id"),
            "last_heartbeat_seconds_ago": snapshot.get("last_heartbeat_seconds_ago"),
        }
    except Exception:
        return {
            "status": "ok",
            "mode": settings.mode,
            "timestamp": datetime.now().isoformat(),
            "version": "2.0.0",
        }


@app.get("/api/health")
async def api_health():
    """Full health endpoint with per-component status. Fast — reads
    in-memory health state maintained by running services."""
    try:
        from backend.health.health_monitor import health_monitor
        snapshot = health_monitor.snapshot()

        # Also include scanner health details
        scanner_health = {}
        try:
            scanner = getattr(app.state, "scanner", None)
            if scanner is not None:
                scanner_health = scanner.health_report()
        except Exception:
            pass

        # WebSocket status
        ws_status = {}
        try:
            ws_client = getattr(app.state, "ws_client", None)
            if ws_client is not None:
                ws_status = ws_client.status_report()
        except Exception:
            pass

        # Supervisor status
        supervisor_status = {}
        try:
            supervisor = getattr(app.state, "supervisor", None)
            if supervisor is not None:
                supervisor_status = supervisor.status()
        except Exception:
            pass

        return {
            "status": "ok",
            "health": snapshot,
            "scanner": scanner_health,
            "websocket": ws_status,
            "supervisor": supervisor_status,
        }
    except Exception:
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
            "upstox_v3_token_approval": True,
            "upstox_notifier_webhook": True,
            "upstox_v3_websocket": True,
            "upstox_python_sdk_installed": sdk_installed,
            "multi_strategy_engine": True,
            "live_scanner": True,
            "universe_selection": True,
            "realistic_backtest_engine": True,
            "option_chain_analysis": True,
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
