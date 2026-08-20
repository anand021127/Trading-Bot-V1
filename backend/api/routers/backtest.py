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
from backend.config.universe_config import VALID_OPTION_INDICES
from backend.database.db_manager import DatabaseManager

logger = logging.getLogger(__name__)
router = APIRouter()
settings = load_settings()
db = DatabaseManager(db_path=settings.database.path)

DEFAULT_SYMBOLS = list(VALID_OPTION_INDICES)

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
    strategies: Optional[List[str]] = None  # option strategy names
    risk_pct_per_trade: float = 0.01


def _get_token() -> str:
    from backend.broker.token_resolver import resolve_upstox_token
    return resolve_upstox_token()


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
    invalid_symbols = [symbol.upper() for symbol in symbols if symbol.upper() not in VALID_OPTION_INDICES]
    if invalid_symbols:
        raise HTTPException(status_code=400, detail={"message": "Backtests support index options only", "invalid_symbols": invalid_symbols})
    if request.strategies and any(name != "OPTION_PREMIUM" for name in request.strategies):
        raise HTTPException(status_code=400, detail="Only OPTION_PREMIUM is supported for options backtests")
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


@router.get("/download/{task_id}")
async def download_backtest_result(task_id: str, format: str = "csv"):
    """Download backtest result as a file.

    Query params:
        format: 'csv' or 'json' (default: 'csv')

    Returns a file download with proper Content-Disposition header.
    """
    from fastapi.responses import FileResponse

    task = task_manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"No backtest task found with id {task_id}")
    if task.status == STATUS_FAILED:
        raise HTTPException(status_code=400, detail="Cannot download results of a failed backtest")
    if task.status != STATUS_COMPLETED:
        raise HTTPException(status_code=400, detail="Backtest is still running — wait for completion before downloading")

    # Build a descriptive filename
    result = task.result or {}
    symbols = result.get("symbols_requested", [])
    date_range = result.get("date_range", {})
    start = date_range.get("start", "").replace("-", "")
    end = date_range.get("end", "").replace("-", "")
    sym_slug = "_".join(s.lower() for s in symbols[:3]) if symbols else "backtest"
    if len(symbols) > 3:
        sym_slug += f"_+{len(symbols) - 3}"
    date_slug = f"_{start}_{end}" if start and end else ""

    fmt = format.lower().strip()
    if fmt == "json":
        path = task.generate_json()
        filename = f"upstox_backtest_{sym_slug}{date_slug}.json"
        media_type = "application/json"
    elif fmt == "csv":
        path = task.generate_csv()
        filename = f"upstox_backtest_{sym_slug}{date_slug}.csv"
        media_type = "text/csv"
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {fmt}. Use 'csv' or 'json'.")

    return FileResponse(
        path=path,
        filename=filename,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
