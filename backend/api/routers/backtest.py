"""Router for initiating and querying backtests."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from ...config.settings import load_settings

router = APIRouter()
settings = load_settings()


class BacktestRequest(BaseModel):
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    commission_pct: Optional[float] = None
    slippage_pct: Optional[float] = None


@router.post("/run")
async def run_backtest(request: BacktestRequest) -> dict:
    return {
        "status": "completed",
        "mode": settings.mode,
        "requested": request.dict(),
        "summary": {
            "total_trades": 0,
            "net_pnl": 0.0,
            "win_rate": 0.0,
            "start_date": request.start_date or settings.backtest.start_date,
            "end_date": request.end_date or settings.backtest.end_date,
            "ran_at": datetime.now().isoformat(),
        },
    }
