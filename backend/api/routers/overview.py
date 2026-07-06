"""Overview endpoint — real-time bot dashboard state."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from fastapi import APIRouter

from ..websocket import manager as websocket_manager
from backend.config.settings import load_settings
from backend.database.db_manager import DatabaseManager
from backend.risk.risk_manager import RiskManager

router = APIRouter()
settings = load_settings()
db_manager = DatabaseManager(db_path=settings.database.path)

# Singleton risk manager (shared state across requests)
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
    """Check if NSE market is currently open (9:15 AM – 3:30 PM IST, Mon–Fri)."""
    now_ist = datetime.now(IST)
    if now_ist.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    market_open  = now_ist.replace(hour=9,  minute=15, second=0, microsecond=0)
    market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now_ist <= market_close


def _serialize_position(row: Any) -> Dict[str, Any]:
    d = dict(row) if hasattr(row, "keys") else row.__dict__
    d.pop("_sa_instance_state", None)
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
async def get_overview() -> Dict[str, Any]:
    today_stats = _get_today_stats()
    risk_status = _risk_manager.get_status()

    positions: List[Dict[str, Any]] = []
    try:
        positions = [_serialize_position(p) for p in db_manager.list_positions()]
    except Exception:
        positions = []

    used_capital = sum(
        float(p.get("average_price", 0) or 0) * int(p.get("quantity", 0) or 0)
        for p in positions
    )
    available_capital = max(0.0, settings.capital.total - used_capital)
    daily_pnl_pct = (today_stats["net_pnl"] / settings.capital.total * 100) if settings.capital.total else 0.0

    return {
        "status": "ok",
        "daily_pnl": {
            "amount": today_stats["net_pnl"],
            "pct": round(daily_pnl_pct, 3),
        },
        "capital": {
            "total": settings.capital.total,
            "available": round(available_capital, 2),
            "used": round(used_capital, 2),
            "buffer": round(settings.capital.cash_buffer * settings.capital.total, 2),
        },
        "today_stats": today_stats,
        "risk_status": risk_status,
        "trend_bias": "NEUTRAL",
        "open_positions": positions,
        "watchlist": [],
        "system": {
            "last_candle_seconds_ago": 0,
            "websocket_connected": len(websocket_manager.active_connections) > 0,
            "active_connections": len(websocket_manager.active_connections),
            "last_api_call": datetime.now(timezone.utc).isoformat(),
            "mode": settings.mode,
            "market_open": _is_market_open(),
        },
    }
