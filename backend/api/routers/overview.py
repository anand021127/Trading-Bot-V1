"""Overview endpoint — real-time bot dashboard state."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request

from ..websocket import manager as websocket_manager
from backend.config.runtime_config import get_effective_settings
from backend.config.settings import load_settings
from backend.database.db_manager import DatabaseManager
from backend.risk.risk_manager import RiskManager

router = APIRouter()
settings = load_settings()  # legacy module-level snapshot (fallback only — see below)
db_manager = DatabaseManager(db_path=settings.database.path)

# Fallback risk manager — used ONLY when no live engine is attached to
# this process (e.g. an isolated test context). When a real engine is
# running, get_overview() below reads ITS risk_manager instead — see the
# root-cause note there: this module-level instance used to be read
# unconditionally, meaning the dashboard's Risk Meter was tracking a
# phantom RiskManager that never received a single record_trade_result()
# call from the actual trading loop, regardless of how many real trades
# executed.
_risk_manager = RiskManager(
    capital=settings.capital.total,
    daily_loss_limit=settings.risk.max_daily_loss_pct,
    max_trades_per_day=settings.risk.max_trades_per_day,
    max_concurrent_positions=settings.risk.max_concurrent_positions,
    max_consecutive_losses=settings.risk.max_consecutive_losses,
    pause_minutes_after_losses=settings.risk.pause_after_losses_minutes,
)

IST = ZoneInfo("Asia/Kolkata")


def _is_market_open() -> bool:
    """Delegates to the ONE authoritative exchange calendar (weekends,
    official NSE/BSE holidays, special sessions) — no local weekday math."""
    from backend.market.calendar import is_market_open_now
    return is_market_open_now()


def _serialize_position(row: Any) -> Dict[str, Any]:
    d = dict(row) if hasattr(row, "keys") else dict(getattr(row, "__dict__", {}))
    d.pop("_sa_instance_state", None)
    # Common trade metadata model — open positions surface the same contract
    # identity as Trade History (from the extra state persisted at entry).
    extra = d.pop("extra", None)
    if not isinstance(extra, dict):
        extra = {}
    d["underlying_symbol"] = d.get("underlying_symbol") or extra.get("underlying")
    d["option_type"] = d.get("option_type") or extra.get("option_type")
    if d.get("strike_price") is None:
        d["strike_price"] = extra.get("strike")
    d["expiry"] = d.get("expiry") or extra.get("expiry")
    d["lot_size"] = d.get("lot_size") or extra.get("lot_size")
    d["trade_id"] = d.get("trade_id") or extra.get("trade_id")
    d["strategy"] = d.get("strategy") or extra.get("strategy")
    entry_price = float(d.get("average_price") or 0)
    qty = int(d.get("quantity") or 0)
    d["entry_price"] = entry_price
    # Capital actually deployed = entry price × executed quantity (the ONE
    # common definition — never allocation or account capital).
    d["capital_used"] = round(entry_price * qty, 2) if entry_price > 0 and qty > 0 else None
    return d


def _get_today_stats() -> Dict[str, Any]:
    """Compute today's P&L, win count, loss count from DB."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        rows = db_manager.list_trades(date_from=today, date_to=today)
        pnl_total = 0.0
        wins = 0
        losses = 0
        for row in rows:
            d = dict(row) if hasattr(row, "keys") else {}
            pnl = float(d.get("net_pnl") or d.get("pnl") or 0)
            pnl_total += pnl
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
        total = wins + losses
        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total * 100, 1) if total else 0.0,
            "net_pnl": round(pnl_total, 2),
        }
    except Exception:
        return {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "net_pnl": 0.0}


@router.get("/overview")
async def get_overview(request: Request) -> Dict[str, Any]:
    # PHASE B — ONE authoritative config. Previously this endpoint used the
    # module-level env snapshot `settings`, so a user who saved capital
    # ₹20,000 in the Settings UI still saw ₹1,00,000 here. Now every request
    # re-resolves Settings-DB-blob-over-env — the SAME authority the trading
    # engine's RiskManager uses (trading_engine.py reads
    # get_effective_settings() when constructing it).
    settings = get_effective_settings()
    today_stats = _get_today_stats()
    # ROOT CAUSE FIX: previously always read the module-level
    # `_risk_manager` above, a completely separate instance from the one
    # TradingEngine actually calls record_trade_result()/
    # record_exposure_closed() on during real trading — so the dashboard's
    # Risk Meter ("0/4 trades", "0 consecutive losses") was structurally
    # incapable of reflecting real risk state no matter what happened in
    # the live engine. Now reads the REAL engine's risk_manager when one
    # is attached to this process (the normal running-bot case), and only
    # falls back to the phantom local instance when none is (e.g. a bare
    # test harness with no engine wired up).
    engine = getattr(request.app.state, "engine", None)
    active_risk_manager = getattr(engine, "risk_manager", None) or _risk_manager
    risk_status = active_risk_manager.get_status()

    positions: List[Dict[str, Any]] = []
    try:
        positions = [_serialize_position(p) for p in db_manager.list_positions()]
    except Exception:
        positions = []

    used_capital = sum(
        float(p.get("average_price", 0) or 0) * int(p.get("quantity", 0) or 0)
        for p in positions
    )
    # Capital definitions are kept DISTINCT (spec §20):
    #   total     = STARTING CAPITAL (authoritative configured capital)
    #   current   = CURRENT EQUITY (realized P&L-adjusted paper equity)
    #   used      = USED CAPITAL (entry price × executed quantity, live positions)
    #   available = AVAILABLE CAPITAL (current equity − deployed, floored at 0)
    #   buffer    = CASH BUFFER (fraction of starting capital held in reserve)
    starting_capital = float(settings.capital.total)
    current_equity: Optional[float] = None
    equity_source = "starting_capital_fallback"
    try:
        import json as _json
        _snap_raw = db_manager.get_setting("paper_equity_snapshot", "") or ""
        if _snap_raw:
            _snap = _json.loads(_snap_raw)
            _eq = float(_snap.get("realized_equity") or 0)
            if _eq > 0:
                current_equity = _eq
                equity_source = "paper_equity_snapshot"
    except Exception:
        current_equity = None
    equity_base = current_equity if current_equity is not None else starting_capital
    available_capital = max(0.0, equity_base - used_capital)
    daily_pnl_pct = (today_stats["net_pnl"] / starting_capital * 100) if starting_capital else 0.0

    # Real Upstox v3 feed status (not the frontend push channel).
    try:
        from backend.api.websocket import get_broker_ws_status
        broker_ws = get_broker_ws_status()
    except Exception:
        broker_ws = {"connection_status": "unknown", "is_connected": False}

    # Universe — what's actually being watched right now.
    watching_count = 0
    universe_mode = "OPTIONS"
    try:
        from backend.config.universe_config import load_universe_config
        from backend.database.db_manager import DatabaseManager as _DB
        uconfig = load_universe_config(db_manager)
        watching_count = len(uconfig.resolve_symbols())
        universe_mode = uconfig.mode
    except Exception:
        pass

    # Live scanner — what it's analyzing right now + its most recent signal.
    currently_analyzing = None
    last_signal: Optional[Dict[str, Any]] = None
    scanner_running = False
    scanner_health: Dict[str, Any] = {}
    try:
        import backend.api.routers.scanner as scanner_module
        scanner = scanner_module._scanner_ref
        if scanner is not None:
            report = scanner.status_report()
            scanner_running = report.get("is_running", False)
            currently_analyzing = report.get("currently_scanning")
            actionable = [r for r in report.get("results", []) if r.get("signal") != "NONE"]
            if actionable:
                last_signal = max(actionable, key=lambda r: r.get("confidence", 0))
            # Real heartbeat-based health report
            scanner_health = scanner.health_report()
    except Exception:
        pass

    # Health monitor — bot uptime, process health, component statuses
    health_data: Dict[str, Any] = {}
    try:
        from backend.health.health_monitor import health_monitor
        health_data = health_monitor.snapshot()
    except Exception:
        pass

    return {
        "status": "ok",
        "daily_pnl": {
            "amount": today_stats["net_pnl"],
            "pct": round(daily_pnl_pct, 3),
        },
        "capital": {
            "total": round(starting_capital, 2),
            "current": round(current_equity, 2) if current_equity is not None else None,
            "available": round(available_capital, 2),
            "used": round(used_capital, 2),
            "buffer": round(settings.capital.cash_buffer * starting_capital, 2),
            "source": "runtime_config",  # resolved via backend/config/runtime_config.py
            "equity_source": equity_source,
        },
        "today_stats": today_stats,
        "risk_status": risk_status,
        "trend_bias": "NEUTRAL",
        "open_positions": positions,
        "watchlist": [],
        "universe": {
            "mode": universe_mode,
            "watching_count": watching_count,
        },
        "scanner": {
            "is_running": scanner_running,
            "currently_analyzing": currently_analyzing,
            "last_signal": last_signal,
            "health": scanner_health,
        },
        "system": {
            "last_candle_seconds_ago": broker_ws.get("last_tick_age_seconds"),
            "websocket_connected": broker_ws.get("is_connected", False),
            "websocket_status": broker_ws.get("connection_status", "unknown"),
            "market_data_status": broker_ws.get("market_data_status", "UNAVAILABLE"),
            "active_frontend_connections": len(websocket_manager.active_connections),
            "last_api_call": datetime.now(timezone.utc).isoformat(),
            "api_health": "ok" if broker_ws.get("connection_status") not in ("auth_failed",) else "degraded",
            "mode": settings.mode,
            "market_open": _is_market_open(),
        },
        "health": {
            "bot_status": health_data.get("bot_status", "UNKNOWN"),
            "uptime_seconds": health_data.get("uptime_seconds", 0),
            "started_at": health_data.get("started_at"),
            "process_id": health_data.get("process_id"),
            "last_heartbeat_seconds_ago": health_data.get("last_heartbeat_seconds_ago"),
            "components": health_data.get("components", {}),
            "recent_events": health_data.get("recent_events", []),
        },
    }
