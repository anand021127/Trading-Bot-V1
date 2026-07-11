"""Backtest router — item #6.

Runs the SAME multi-strategy engine used live against real, complete
historical candle history. There is no synthetic-data fallback anywhere in
this file — if there's no valid token or Upstox returns no usable candles,
the response says so explicitly (`data_source` + `skipped_symbols`), it
never fabricates trades to avoid an empty result.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.backtest.engine import BacktestEngine, CostConfig
from backend.config.settings import load_settings
from backend.config.universe_config import NIFTY50_SYMBOLS
from backend.database.db_manager import DatabaseManager

logger = logging.getLogger(__name__)
router = APIRouter()
settings = load_settings()
db = DatabaseManager(db_path=settings.database.path)

DEFAULT_SYMBOLS = NIFTY50_SYMBOLS[:5]


class BacktestRequest(BaseModel):
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    commission_pct: Optional[float] = None
    slippage_pct: Optional[float] = None
    stt_pct: Optional[float] = None
    symbols: Optional[List[str]] = None
    capital: Optional[float] = None
    interval: str = "5minute"           # 1minute|3minute|5minute|15minute|30minute|day
    strategies: Optional[List[str]] = None  # subset of EMA_TREND / ORB
    risk_pct_per_trade: float = 0.01


def _get_token() -> str:
    import os
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "")
    if not token:
        try:
            token = db.load_token()
        except Exception:
            token = ""
    return token


@router.post("/run")
async def run_backtest(request: BacktestRequest) -> Dict[str, Any]:
    token = _get_token()
    if not token:
        raise HTTPException(
            status_code=400,
            detail="No Upstox access token configured. Backtesting requires real "
                   "historical data — there is no synthetic-data mode. Go to "
                   "Settings and connect your token.",
        )

    from backend.broker.upstox_client import UpstoxClient, UpstoxAPIError

    client = UpstoxClient(access_token=token)
    capital = request.capital or settings.capital.total
    symbols = request.symbols or DEFAULT_SYMBOLS
    start_date = request.start_date or settings.backtest.start_date
    end_date = request.end_date or settings.backtest.end_date

    symbol_candles: Dict[str, List[Dict[str, Any]]] = {}
    fetch_errors: List[Dict[str, str]] = []

    for sym in symbols:
        try:
            candles = client.get_historical_candles_full_range(
                sym, request.interval, from_date=start_date, to_date=end_date,
            )
            symbol_candles[sym] = candles
        except UpstoxAPIError as e:
            fetch_errors.append({"symbol": sym, "error": str(e)})
            logger.warning("Backtest candle fetch failed for %s: %s", sym, e)

    if not symbol_candles or all(len(c) == 0 for c in symbol_candles.values()):
        raise HTTPException(
            status_code=502,
            detail=f"Could not fetch any real historical candles for {symbols}. "
                   f"Errors: {fetch_errors}. Refusing to fabricate results.",
        )

    costs = CostConfig(
        commission_pct=request.commission_pct if request.commission_pct is not None else settings.backtest.commission_pct,
        slippage_pct=request.slippage_pct if request.slippage_pct is not None else settings.backtest.slippage_pct,
        stt_pct=request.stt_pct if request.stt_pct is not None else settings.backtest.stt_pct,
    )
    engine = BacktestEngine(
        costs=costs, capital=capital, risk_pct_per_trade=request.risk_pct_per_trade,
    )

    result = engine.run(symbol_candles, strategy_names=request.strategies)
    payload = result.to_dict()
    payload["fetch_errors"] = fetch_errors
    payload["symbols_requested"] = symbols
    payload["date_range"] = {"start": start_date, "end": end_date}
    payload["interval"] = request.interval

    if result.trades_taken == 0 and not result.skipped_symbols:
        payload["message"] = (
            "Real candle data was processed but no strategy conditions were met "
            "in this window — see rejection_reason_counts for exactly why. This "
            "is a genuine result, not an error."
        )

    return payload


@router.get("/status/{task_id}")
async def get_backtest_status(task_id: str) -> Dict[str, Any]:
    """`/run` is synchronous and returns the full result directly — this
    endpoint exists only so older frontend polling code doesn't 404."""
    return {"task_id": task_id, "status": "completed", "progress_pct": 100}


@router.get("/result/{task_id}")
async def get_backtest_result(task_id: str) -> Dict[str, Any]:
    return {"task_id": task_id, "status": "completed", "trade_log": [],
            "message": "Results are returned directly by POST /api/backtest/run."}
