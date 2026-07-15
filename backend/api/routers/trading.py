"""Trades, positions, and prices API router."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from backend.config.settings import load_settings
from backend.database.db_manager import DatabaseManager

logger = logging.getLogger(__name__)
router  = APIRouter()
settings = load_settings()
db = DatabaseManager(db_path=settings.database.path)
IST = ZoneInfo("Asia/Kolkata")

NIFTY50_SYMBOLS = [
    "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR","KOTAKBANK",
    "LT","SBIN","AXISBANK","BHARTIARTL","ITC","ASIANPAINT","MARUTI","HCLTECH",
    "SUNPHARMA","WIPRO","TITAN","ULTRACEMCO","BAJFINANCE","NESTLEIND","TECHM",
    "NTPC","POWERGRID","ONGC","JSWSTEEL","TATASTEEL","HINDALCO","TATAMOTORS",
    "M&M","BAJAJFINSV","DRREDDY","CIPLA","DIVISLAB","APOLLOHOSP","ADANIENT",
    "ADANIPORTS","COALINDIA","BPCL","EICHERMOT","HEROMOTOCO","INDUSINDBK",
    "SBILIFE","HDFCLIFE","GRASIM","TATACONSUM","UPL","BRITANNIA","SHREECEM","BAJAJ-AUTO",
]


def _is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    open_t  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t


def _row_to_dict(row: Any) -> Dict[str, Any]:
    d = dict(row) if hasattr(row, "keys") else {}
    d.pop("_sa_instance_state", None)
    for k in ("entry_time", "exit_time", "timestamp", "created_at"):
        if isinstance(d.get(k), datetime):
            d[k] = d[k].isoformat()
    return d


def _empty_quote(symbol: str) -> Dict[str, Any]:
    return {
        "symbol": symbol, "ltp": 0.0, "open": 0.0, "high": 0.0,
        "low": 0.0, "close": 0.0, "volume": 0, "change": 0.0, "change_pct": 0.0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market_closed": True,
        "has_data": False,  # explicit: this is a placeholder, not a real ₹0.00 price
    }


def _get_token() -> str:
    """Get token from env first, then DB fallback."""
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "")
    if not token:
        try:
            token = db.load_token()
            if token:
                os.environ["UPSTOX_ACCESS_TOKEN"] = token
        except Exception:
            pass
    return token


def _merge_live_ws_ticks(result: Dict[str, Any], symbols: List[str]) -> Dict[str, Any]:
    """Overlay real-time Upstox v3 WebSocket ticks (if connected) on top of
    the REST snapshot, so ltp/change/change_pct/volume reflect the latest
    tick instead of the last poll. Never fabricates data."""
    try:
        from backend.api.websocket import get_prices_by_symbol
        live = get_prices_by_symbol()
    except Exception:
        return result

    for sym in symbols:
        tick = live.get(sym)
        if not tick or not tick.get("ltp"):
            continue
        base = result.setdefault(sym, _empty_quote(sym))
        base["ltp"] = tick["ltp"]
        base["change"] = tick.get("change", base.get("change", 0.0))
        base["change_pct"] = tick.get("change_pct", base.get("change_pct", 0.0))
        if tick.get("volume"):
            base["volume"] = tick["volume"]
        if tick.get("prev_close"):
            base["close"] = tick["prev_close"]
        base["market_closed"] = False
        base["source"] = "websocket"
    return result


def _fetch_prices(symbols: List[str]) -> Dict[str, Any]:
    """Fetch prices from Upstox. Returns empty quotes on failure — never
    fabricated/mock values."""
    token = _get_token()
    if not token:
        return {s: _empty_quote(s) for s in symbols}
    try:
        from backend.broker.upstox_client import UpstoxClient
        client = UpstoxClient(access_token=token)
        result = client.get_multiple_quotes(symbols)
        market_open = _is_market_open()
        for sym in result:
            result[sym]["market_closed"] = not market_open
            result[sym].setdefault("source", "rest")
    except Exception as e:
        logger.debug("Price fetch error: %s", e)
        result = {s: _empty_quote(s) for s in symbols}

    return _merge_live_ws_ticks(result, symbols)


# ─── Trades ──────────────────────────────────────────────────────────────────

@router.get("/trades")
async def get_trades(
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
    symbol:    Optional[str] = Query(None),
    mode:      Optional[str] = Query(None),
    exit_reason: Optional[str] = Query(None),
    page:      int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> Dict[str, Any]:
    try:
        rows = db.list_trades(
            date_from=date_from, date_to=date_to,
            symbol=symbol, mode=mode, exit_reason=exit_reason,
        )
    except Exception as e:
        logger.error("list_trades error: %s", e)
        rows = []

    total     = len(rows)
    page_rows = rows[(page - 1) * page_size: page * page_size]
    trades    = [_row_to_dict(r) for r in page_rows]

    all_dicts = [_row_to_dict(r) for r in rows]
    net_pnls  = [float(d.get("net_pnl") or d.get("pnl") or 0) for d in all_dicts]
    wins      = [p for p in net_pnls if p > 0]
    losses    = [p for p in net_pnls if p < 0]

    return {
        "trades": trades,
        "total_count": total,
        "summary": {
            "total_trades":  total,
            "total_net_pnl": round(sum(net_pnls), 2),
            "win_rate":      round(len(wins) / total * 100, 2) if total else 0.0,
            "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses else 0.0,
            "avg_win":       round(sum(wins) / len(wins), 2) if wins else 0.0,
            "avg_loss":      round(abs(sum(losses)) / len(losses), 2) if losses else 0.0,
        },
    }


@router.get("/trades/export/csv")
async def export_trades_csv(
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
    symbol:    Optional[str] = Query(None),
) -> StreamingResponse:
    try:
        rows = db.list_trades(date_from=date_from, date_to=date_to, symbol=symbol)
    except Exception:
        rows = []

    cols = [
        "id","symbol","mode","entry_time","exit_time","entry_price","exit_price",
        "quantity","initial_stop","final_stop","exit_reason","gross_pnl","net_pnl",
        "brokerage","stt","pnl_r","trade_duration_min","stage_at_exit",
        "orb_high","orb_low","atr_at_entry","rsi_at_entry","choppiness_at_entry",
        "volume_ratio","ema20_at_entry","ema50_at_entry","trend_bias",
        "max_favorable","max_adverse",
    ]

    def generate():
        yield ",".join(cols) + "\n"
        for r in rows:
            d = _row_to_dict(r)
            yield ",".join(str(d.get(c, "")) for c in cols) + "\n"

    return StreamingResponse(
        generate(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trades.csv"},
    )


@router.get("/trades/{trade_id}")
async def get_trade(trade_id: str) -> Dict[str, Any]:
    try:
        row = db.get_trade(trade_id)
        return _row_to_dict(row) if row else {"error": "not found"}
    except Exception as e:
        return {"error": str(e)}


# ─── Positions ───────────────────────────────────────────────────────────────

@router.get("/positions")
async def get_positions() -> List[Dict[str, Any]]:
    try:
        return [_row_to_dict(r) for r in db.list_positions()]
    except Exception:
        return []


@router.post("/positions/{symbol}/exit")
async def manual_exit_position(symbol: str) -> Dict[str, Any]:
    return {"status": "exit_queued", "symbol": symbol, "reason": "MANUAL_EXIT"}


@router.get("/positions/live")
async def get_live_positions_detail() -> Dict[str, Any]:
    """Item #7 — for every open position: symbol, entry price, target,
    stop-loss, live-updated trailing SL, current P&L, strategy used."""
    import backend.api.routers.bot_control as bot_control_module
    engine = bot_control_module._engine_ref
    if engine is None:
        return {"positions": [], "mode": settings.mode,
                "note": "Trading engine not initialized (no Upstox token configured?)"}
    try:
        return {"positions": engine.get_open_positions_detail(), "mode": settings.mode}
    except Exception as e:
        logger.exception("Failed to build live positions detail")
        return {"positions": [], "mode": settings.mode, "error": str(e)}


# ─── Prices ──────────────────────────────────────────────────────────────────

@router.get("/prices/live")
async def get_live_prices() -> Dict[str, Any]:
    watchlist = NIFTY50_SYMBOLS[:settings.universe.max_stocks_in_watchlist]
    prices = _fetch_prices(watchlist)
    return {
        "prices": prices,
        "market_open": _is_market_open(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "token_present": bool(_get_token()),
    }


@router.get("/prices/nifty50")
async def get_nifty50_prices() -> Dict[str, Any]:
    prices = _fetch_prices(NIFTY50_SYMBOLS)
    return {
        "prices": prices,
        "market_open": _is_market_open(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "token_present": bool(_get_token()),
    }


INDEX_SYMBOLS = ["NIFTY50", "BANKNIFTY", "SENSEX"]


@router.get("/prices/indices")
async def get_index_prices() -> Dict[str, Any]:
    """NIFTY 50, BANKNIFTY, SENSEX — indices have no traded volume, so that
    field is always 0/omitted for these, which is correct (not a bug)."""
    prices = _fetch_prices(INDEX_SYMBOLS)
    return {
        "prices": prices,
        "market_open": _is_market_open(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "token_present": bool(_get_token()),
    }

# NOTE: paper trading status now lives exclusively in
# backend/api/routers/paper.py (GET /api/paper/status), which computes
# real values from the actual trade log via
# backend/paper/status_calculator.py. An earlier duplicate endpoint used
# to live here with the same path, registered before paper_router — it
# was fully shadowed/unreachable (Starlette matches routes in
# registration order) and also hardcoded several checklist fields to a
# fake `{"pass": True}` regardless of real data, which is exactly the
# kind of fabricated-data issue this project has otherwise been fixed to
# avoid. Removed rather than left as dead code so it can never
# accidentally become live again if routers are ever reordered.
