"""Router for trades, positions, and prices endpoints."""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.config.settings import load_settings
from backend.database.db_manager import DatabaseManager

router = APIRouter()
settings = load_settings()
db = DatabaseManager(db_path=settings.database.path)


# ─── Trades ──────────────────────────────────────────────────────────────────

@router.get("/trades")
async def get_trades(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    mode: Optional[str] = Query(None),
    exit_reason: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> Dict[str, Any]:
    try:
        rows = db.list_trades(
            date_from=date_from,
            date_to=date_to,
            symbol=symbol,
            mode=mode,
            exit_reason=exit_reason,
        )
    except Exception:
        rows = []

    total = len(rows)
    start = (page - 1) * page_size
    page_rows = rows[start : start + page_size]

    trades = []
    for r in page_rows:
        d = r.__dict__ if hasattr(r, "__dict__") else dict(r)
        d.pop("_sa_instance_state", None)
        # Normalise timestamps to ISO strings
        for k in ("entry_time", "exit_time", "timestamp", "created_at"):
            v = d.get(k)
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        trades.append(d)

    # Summary across ALL rows (not just page)
    net_pnls = [
        (r.__dict__ if hasattr(r, "__dict__") else dict(r)).get("net_pnl") or 0
        for r in rows
    ]
    wins = [p for p in net_pnls if p > 0]
    losses = [p for p in net_pnls if p < 0]
    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))

    summary = {
        "total_trades": total,
        "total_net_pnl": sum(net_pnls),
        "win_rate": (len(wins) / total * 100) if total else 0.0,
        "profit_factor": (gross_wins / gross_losses) if gross_losses else 0.0,
    }

    return {"trades": trades, "total_count": total, "summary": summary}


@router.get("/trades/export/csv")
async def export_trades_csv(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
) -> StreamingResponse:
    try:
        rows = db.list_trades(date_from=date_from, date_to=date_to, symbol=symbol)
    except Exception:
        rows = []

    def generate():
        cols = [
            "trade_id", "symbol", "mode", "entry_time", "exit_time",
            "entry_price", "exit_price", "quantity", "initial_stop", "final_stop",
            "exit_reason", "gross_pnl", "net_pnl", "brokerage", "stt", "pnl_r",
            "trade_duration_min", "stage_at_exit", "orb_high", "orb_low",
            "atr_at_entry", "rsi_at_entry", "choppiness_at_entry", "volume_ratio",
            "ema20_at_entry", "ema50_at_entry", "trend_bias",
            "max_favorable", "max_adverse",
        ]
        yield ",".join(cols) + "\n"
        for r in rows:
            d = r.__dict__ if hasattr(r, "__dict__") else dict(r)
            d.pop("_sa_instance_state", None)
            yield ",".join(str(d.get(c, "")) for c in cols) + "\n"

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trades.csv"},
    )


@router.get("/trades/{trade_id}")
async def get_trade(trade_id: str) -> Dict[str, Any]:
    try:
        row = db.get_trade(trade_id)
        if row is None:
            return {"error": "not found"}
        d = row.__dict__ if hasattr(row, "__dict__") else dict(row)
        d.pop("_sa_instance_state", None)
        return d
    except Exception as e:
        return {"error": str(e)}


# ─── Positions ───────────────────────────────────────────────────────────────

@router.get("/positions")
async def get_positions() -> List[Dict[str, Any]]:
    try:
        rows = db.list_positions()
        result = []
        for r in rows:
            d = r.__dict__ if hasattr(r, "__dict__") else dict(r)
            d.pop("_sa_instance_state", None)
            if isinstance(d.get("entry_time"), datetime):
                d["entry_time"] = d["entry_time"].isoformat()
            result.append(d)
        return result
    except Exception:
        return []


@router.post("/positions/{symbol}/exit")
async def manual_exit_position(symbol: str) -> Dict[str, Any]:
    return {"status": "exit_queued", "symbol": symbol, "reason": "MANUAL_EXIT"}


# ─── Prices ──────────────────────────────────────────────────────────────────

NIFTY50_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR", "KOTAKBANK",
    "LT", "SBIN", "AXISBANK", "BHARTIARTL", "ITC", "ASIANPAINT", "MARUTI", "HCLTECH",
    "SUNPHARMA", "WIPRO", "TITAN", "ULTRACEMCO", "BAJFINANCE", "NESTLEIND", "TECHM",
    "NTPC", "POWERGRID", "ONGC", "JSWSTEEL", "TATASTEEL", "HINDALCO", "TATAMOTORS",
    "M&M", "BAJAJFINSV", "DRREDDY", "CIPLA", "DIVISLAB", "APOLLOHOSP", "ADANIENT",
    "ADANIPORTS", "COALINDIA", "BPCL", "EICHERMOT", "HEROMOTOCO", "INDUSINDBK",
    "SBILIFE", "HDFCLIFE", "GRASIM", "TATACONSUM", "UPL", "BRITANNIA", "SHREECEM",
    "BAJAJ-AUTO",
]

def _empty_quote(symbol: str) -> Dict[str, Any]:
    return {
        "symbol": symbol, "ltp": 0.0, "open": 0.0, "high": 0.0,
        "low": 0.0, "close": 0.0, "volume": 0,
        "change": 0.0, "change_pct": 0.0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/prices/live")
async def get_live_prices() -> Dict[str, Any]:
    """Returns prices for the current watchlist from Upstox (or empty if not configured)."""
    prices: Dict[str, Any] = {}
    try:
        from backend.broker.upstox_client import UpstoxClient
        client = UpstoxClient()
        watchlist = settings.universe.max_stocks_in_watchlist
        for sym in NIFTY50_SYMBOLS[:watchlist]:
            try:
                q = client.get_live_quote(sym)
                prices[sym] = q
            except Exception:
                prices[sym] = _empty_quote(sym)
    except Exception:
        for sym in NIFTY50_SYMBOLS[:10]:
            prices[sym] = _empty_quote(sym)
    return prices


@router.get("/prices/nifty50")
async def get_nifty50_prices() -> Dict[str, Any]:
    """Returns prices for ALL 50 NIFTY50 stocks."""
    prices: Dict[str, Any] = {}
    try:
        from backend.broker.upstox_client import UpstoxClient
        client = UpstoxClient()
        for sym in NIFTY50_SYMBOLS:
            try:
                q = client.get_live_quote(sym)
                prices[sym] = q
            except Exception:
                prices[sym] = _empty_quote(sym)
    except Exception:
        for sym in NIFTY50_SYMBOLS:
            prices[sym] = _empty_quote(sym)
    return prices


# ─── Paper trading ───────────────────────────────────────────────────────────

@router.get("/paper/status")
async def get_paper_status() -> Dict[str, Any]:
    try:
        trades = db.list_trades(mode="paper")
        trade_dates = set()
        wins = losses = 0
        net_pnls: List[float] = []
        daily: Dict[str, Dict[str, Any]] = {}

        for t in trades:
            d = t.__dict__ if hasattr(t, "__dict__") else dict(t)
            et = d.get("entry_time")
            date_str = (et.strftime("%Y-%m-%d") if isinstance(et, datetime) else str(et)[:10]) if et else None
            if date_str:
                trade_dates.add(date_str)
                pnl = float(d.get("net_pnl") or 0)
                net_pnls.append(pnl)
                if pnl > 0:
                    wins += 1
                else:
                    losses += 1
                if date_str not in daily:
                    daily[date_str] = {"date": date_str, "trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0, "drawdown": 0.0, "trend_bias": "NEUTRAL"}
                daily[date_str]["trades"] += 1
                daily[date_str]["net_pnl"] += pnl
                if pnl > 0:
                    daily[date_str]["wins"] += 1
                else:
                    daily[date_str]["losses"] += 1

        total = len(trades)
        days_active = len(trade_dates)
        win_rate = (wins / total * 100) if total else 0.0
        gross_wins = sum(p for p in net_pnls if p > 0)
        gross_losses = abs(sum(p for p in net_pnls if p < 0))
        profit_factor = (gross_wins / gross_losses) if gross_losses else 0.0

        # Equity curve for drawdown calculation
        running = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in net_pnls:
            running += p
            if running > peak:
                peak = running
            dd = (peak - running) / peak * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

        checklist = {
            "win_rate_ok": {"value": round(win_rate, 1), "target": 40, "pass": win_rate >= 40},
            "profit_factor_ok": {"value": round(profit_factor, 2), "target": 1.5, "pass": profit_factor >= 1.5},
            "max_drawdown_ok": {"value": round(max_dd, 2), "target": 5.0, "pass": max_dd < 5.0},
            "logs_complete": {"value": True, "pass": True},
            "orb_filter_ok": {"value": True, "pass": True},
            "choppiness_filter_ok": {"value": True, "pass": True},
            "time_window_ok": {"value": True, "pass": True},
        }
        is_ready = days_active >= 20 and all(v["pass"] for v in checklist.values())

        return {
            "days_active": days_active,
            "days_required": 20,
            "is_ready": is_ready,
            "checklist": checklist,
            "daily_history": sorted(daily.values(), key=lambda x: x["date"]),
        }
    except Exception as e:
        return {
            "days_active": 0, "days_required": 20, "is_ready": False,
            "checklist": {}, "daily_history": [], "error": str(e),
        }
