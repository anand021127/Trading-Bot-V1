"""Router for backtesting."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from backend.config.settings import load_settings

router = APIRouter()
settings = load_settings()


class BacktestRequest(BaseModel):
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    commission_pct: Optional[float] = None
    slippage_pct: Optional[float] = None
    symbols: Optional[List[str]] = None
    capital: Optional[float] = None


@router.post("/run")
async def run_backtest(request: BacktestRequest) -> Dict[str, Any]:
    """
    Run a backtest. Currently returns a stub response.
    Full implementation requires historical data from Upstox API.
    """
    capital = request.capital or settings.capital.total
    return {
        "status": "completed",
        "total_trades": 0,
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "net_profit": 0.0,
        "max_drawdown_pct": 0.0,
        "sharpe_ratio": 0.0,
        "avg_win_r": 0.0,
        "avg_loss_r": 0.0,
        "trade_log": [],
        "equity_curve": [
            {"date": request.start_date or settings.backtest.start_date, "value": capital},
            {"date": request.end_date or settings.backtest.end_date, "value": capital},
        ],
        "monthly_returns": {},
        "message": "Backtest engine requires live Upstox historical data. Configure your API token and retry.",
    }


@router.get("/status/{task_id}")
async def get_backtest_status(task_id: str) -> Dict[str, Any]:
    return {"task_id": task_id, "status": "completed", "progress_pct": 100}


@router.get("/result/{task_id}")
async def get_backtest_result(task_id: str) -> Dict[str, Any]:
    return {"task_id": task_id, "status": "completed", "trade_log": []}
