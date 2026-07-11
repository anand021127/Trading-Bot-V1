"""WebSocket connection manager — pushes live prices and bot state to frontend."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set
from zoneinfo import ZoneInfo

from fastapi import WebSocket

from backend.config.settings import load_settings

logger = logging.getLogger(__name__)
settings = load_settings()
IST = ZoneInfo("Asia/Kolkata")


class ConnectionManager:
    """Thread-safe WebSocket connection manager."""

    def __init__(self) -> None:
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.add(websocket)
        logger.debug("WS client connected. Total: %d", len(self.active_connections))

    def disconnect(self, websocket: WebSocket) -> None:
        self.active_connections.discard(websocket)
        logger.debug("WS client disconnected. Total: %d", len(self.active_connections))

    async def send_to(self, websocket: WebSocket, data: Dict[str, Any]) -> None:
        try:
            await websocket.send_json(data)
        except Exception:
            self.disconnect(websocket)

    async def broadcast(self, data: Dict[str, Any]) -> None:
        if not self.active_connections:
            return
        dead = set()
        for ws in list(self.active_connections):
            try:
                await ws.send_json(data)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()

# Price cache populated by the broker's Upstox v3 WebSocket client.
# Keyed by instrument_key (e.g. "NSE_EQ|INE002A01018", "NSE_INDEX|Nifty 50").
_price_cache: Dict[str, Any] = {}

# instrument_key -> friendly symbol (e.g. "RELIANCE", "NIFTY50"), populated
# lazily from backend.broker.upstox_client.ALL_INSTRUMENTS so the cache below
# can also be looked up/broadcast by symbol for the frontend.
_key_to_symbol: Dict[str, str] = {}


def _ensure_symbol_map() -> None:
    if _key_to_symbol:
        return
    try:
        from backend.broker.upstox_client import ALL_INSTRUMENTS
        for symbol, key in ALL_INSTRUMENTS.items():
            _key_to_symbol[key] = symbol
    except Exception:
        pass


def update_price_cache(prices: Dict[str, Any]) -> None:
    """Called by the broker's Upstox v3 WebSocket client on every tick batch.
    `prices` is keyed by instrument_key; we mirror it into a by-symbol view
    too so REST/WS consumers can look either up."""
    _ensure_symbol_map()
    _price_cache.update(prices)


def get_prices_by_symbol() -> Dict[str, Any]:
    _ensure_symbol_map()
    return {
        _key_to_symbol.get(k, k): v
        for k, v in _price_cache.items()
    }


def get_broker_ws_status() -> Dict[str, Any]:
    """Real connection status of the Upstox v3 feed (not the frontend push
    channel). Populated via the app-level ws_client if available."""
    try:
        import backend.api.main as main_mod  # avoid circular import at module load
        client = getattr(getattr(main_mod, "app", None), "state", None)
        client = getattr(client, "ws_client", None)
        if client is not None:
            return client.status_report()
    except Exception:
        pass
    return {"connection_status": "unknown", "is_connected": False}


def _is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    open_t  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t


def _get_bot_state() -> Dict[str, Any]:
    try:
        from backend.strategy.trading_engine import BotState
        return BotState.status()
    except Exception:
        return {"running": False, "kill_switch_active": False}


def _get_scanner_state() -> Dict[str, Any]:
    """Live scanner snapshot (currently-analyzing + per-symbol decisions).
    Returns an empty snapshot until the scanner has run at least once."""
    try:
        from backend.strategy.scanner_state import scanner_state
        return scanner_state.snapshot()
    except Exception:
        return {"currently_scanning": None, "last_scan_time": None, "symbols": {}}


def _get_positions() -> list:
    try:
        from backend.database.db_manager import DatabaseManager
        db = DatabaseManager(db_path=settings.database.path)
        rows = db.list_positions()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _common_payload() -> Dict[str, Any]:
    """Shared payload for both initial_state and price_update messages, so the
    two stay in lock-step. `feed_status` is the REAL Upstox v3 feed status (the
    broker socket), distinct from the frontend push channel."""
    bot_state = _get_bot_state()
    broker_status = get_broker_ws_status()
    scanner = _get_scanner_state()
    return {
        "mode": settings.mode,
        "market_open": _is_market_open(),
        # Real Upstox v3 feed status — NOT the frontend push channel.
        "websocket_connected": broker_status.get("is_connected", False),
        "websocket_status": broker_status.get("connection_status", "unknown"),
        "last_tick_age_seconds": broker_status.get("last_tick_age_seconds"),
        "last_tick_time": broker_status.get("last_tick_time"),
        "feed_status": broker_status,
        "active_frontend_connections": len(manager.active_connections),
        "positions": _get_positions(),
        "prices": get_prices_by_symbol(),
        "scanner": scanner,
        "bot_running": bot_state.get("running", False),
        "kill_switch_active": bot_state.get("kill_switch_active", False),
    }


def build_price_update() -> Dict[str, Any]:
    return {
        "type": "price_update",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": _common_payload(),
    }


def build_initial_state() -> Dict[str, Any]:
    return {
        "type": "initial_state",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": _common_payload(),
    }


async def broadcast_price_update() -> None:
    """Called by APScheduler every 5 seconds to push state to all clients."""
    if not manager.active_connections:
        return
    try:
        data = build_price_update()
        await manager.broadcast(data)
    except Exception as e:
        logger.debug("Broadcast error: %s", e)
