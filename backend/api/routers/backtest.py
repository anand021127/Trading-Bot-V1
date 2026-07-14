"""Backtest router — item #6, now with real background job execution.

Root cause of "timeout of 30000ms exceeded": the frontend's axios client
has a 30s request timeout, and running a full year of 5-minute NIFTY data
synchronously inside POST /run (chunked Upstox fetches + the full
multi-strategy simulation over every bar) routinely took well over 30s.

Fix: POST /run now only starts a background asyncio task and returns a
task_id immediately. Progress is polled via GET /status/{task_id}, and the
final result is fetched via GET /result/{task_id} once status=='completed'.

There is still no synthetic-data fallback anywhere in this file — if
there's no valid token or Upstox returns no usable candles for every
symbol, the task fails explicitly with that reason.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.backtest.engine import BacktestEngine, CostConfig
from backend.backtest.task_manager import (
    task_manager,
    run_backtest_in_background,
    STATUS_COMPLETED,
    STATUS_FAILED,
)
from backend.config.settings import load_settings
from backend.config.universe_config import NIFTY50_SYMBOLS
from backend.database.db_manager import DatabaseManager

logger = logging.getLogger(__name__)
router = APIRouter()
settings = load_settings()
db = DatabaseManager(db_path=settings.database.path)

DEFAULT_SYMBOLS = NIFTY50_SYMBOLS[:5]

# Python's asyncio docs explicitly warn: "Save a reference to the result
# of this function, to avoid a task disappearing mid-execution" — a task
# created with asyncio.create_task() and never referenced elsewhere is
# only weakly held by the event loop and can be garbage-collected before
# it completes. This set holds a strong reference until each task finishes.
_background_tasks: set = set()


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
async def start_backtest(request: BacktestRequest) -> Dict[str, Any]:
    """Starts the backtest in the background and returns immediately with
    a task_id — poll GET /status/{task_id} then GET /result/{task_id}."""
    token = _get_token()
    if not token:
        raise HTTPException(
            status_code=400,
            detail="No Upstox access token configured. Backtesting requires real "
                   "historical data — there is no synthetic-data mode. Go to "
                   "Settings and connect your token.",
        )

    from backend.broker.upstox_client import UpstoxClient

    client = UpstoxClient(access_token=token)
    capital = request.capital or settings.capital.total
    symbols = request.symbols or DEFAULT_SYMBOLS
    start_date = request.start_date or settings.backtest.start_date
    end_date = request.end_date or settings.backtest.end_date

    costs = CostConfig(
        commission_pct=request.commission_pct if request.commission_pct is not None else settings.backtest.commission_pct,
        slippage_pct=request.slippage_pct if request.slippage_pct is not None else settings.backtest.slippage_pct,
        stt_pct=request.stt_pct if request.stt_pct is not None else settings.backtest.stt_pct,
    )
    engine = BacktestEngine(
        costs=costs, capital=capital, risk_pct_per_trade=request.risk_pct_per_trade,
    )

    task = task_manager.create_task()
    bg_task = asyncio.create_task(run_backtest_in_background(
        task.task_id, client, engine, symbols, request.interval,
        start_date, end_date, request.strategies,
    ))
    _background_tasks.add(bg_task)
    bg_task.add_done_callback(_background_tasks.discard)

    return {
        "task_id": task.task_id,
        "status": task.status,
        "message": "Backtest started in the background. Poll /status/{task_id} for progress.",
    }


@router.get("/status/{task_id}")
async def get_backtest_status(task_id: str) -> Dict[str, Any]:
    task = task_manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"No backtest task found with id {task_id}")
    return task.to_status_dict()


@router.get("/result/{task_id}")
async def get_backtest_result(task_id: str) -> Dict[str, Any]:
    task = task_manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"No backtest task found with id {task_id}")
    if task.status == STATUS_FAILED:
        raise HTTPException(status_code=502, detail=task.error or "Backtest failed")
    if task.status != STATUS_COMPLETED:
        return {"task_id": task_id, "status": task.status, "progress": task.progress,
                "message": "Backtest still running — poll /status/{task_id} until status is 'completed'."}
    return task.result
