from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter

from ..websocket import manager as websocket_manager
from backend.config.settings import load_settings
from backend.database.db_manager import DatabaseManager

router = APIRouter()
settings = load_settings()
db_manager = DatabaseManager(db_path=settings.database.path)


def _serialize_position(position: Any) -> Dict[str, Any]:
    return {
        "symbol": position.symbol,
        "quantity": position.quantity,
        "average_price": position.average_price,
        "entry_time": position.entry_time.isoformat()
        if hasattr(position.entry_time, "isoformat")
        else str(position.entry_time),
        "side": position.side,
        "unrealized_pnl": position.unrealized_pnl,
    }


@router.get("/overview")
async def get_overview() -> dict:
    positions = []
    try:
        positions = [
            _serialize_position(position) for position in db_manager.list_positions()
        ]
    except Exception:
        positions = []

    return {
        "status": "ok",
        "daily_pnl": {"amount": 0.0, "pct": 0.0},
        "capital": {
            "total": settings.capital.total,
            "available": settings.capital.total,
            "used": 0.0,
            "buffer": settings.capital.cash_buffer * settings.capital.total,
        },
        "today_stats": {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
        },
        "risk_status": {
            "is_trading_allowed": True,
            "status": "ACTIVE",
            "consecutive_losses": 0,
            "daily_loss_used_pct": 0.0,
            "trades_used": 0,
            "max_trades": settings.risk.max_trades_per_day,
            "stop_reason": None,
        },
        "trend_bias": "NEUTRAL",
        "open_positions": positions,
        "watchlist": [],
        "system": {
            "last_candle_seconds_ago": 0,
            "websocket_connected": len(websocket_manager.active_connections) > 0,
            "active_connections": len(websocket_manager.active_connections),
            "last_api_call": datetime.now().isoformat(),
            "mode": settings.mode,
            "market_open": False,
        },
    }
