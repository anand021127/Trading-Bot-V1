"""Performance metrics and analytics router."""
from __future__ import annotations

import math
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query

from backend.config.settings import load_settings
from backend.database.db_manager import DatabaseManager

router = APIRouter()
settings = load_settings()
logger = logging.getLogger(__name__)
db = DatabaseManager(db_path=settings.database.path)


def _row_to_dict(row: Any) -> Dict[str, Any]:
    d = dict(row) if hasattr(row, "keys") else {}
    d.pop("_sa_instance_state", None)
    return d


@router.get("/")
async def get_performance(
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
) -> Dict[str, Any]:
    try:
        trades = db.list_trades(date_from=date_from, date_to=date_to)
    except Exception as e:
        logger.error("Performance load error: %s", e)
        return {"metrics": None, "equity_curve": [], "monthly_returns": {}}

    if not trades:
        return {"metrics": None, "equity_curve": [], "monthly_returns": {}}

    net_pnls: List[float] = []
    daily_pnl: Dict[str, float] = {}

    for t in trades:
        d = _row_to_dict(t)
        pnl = float(d.get("net_pnl") or d.get("pnl") or 0)
        net_pnls.append(pnl)
        et = d.get("entry_time") or d.get("timestamp")
        date_str = str(et)[:10] if et else None
        if date_str:
            daily_pnl[date_str] = daily_pnl.get(date_str, 0.0) + pnl

    total = len(net_pnls)
    wins   = [p for p in net_pnls if p > 0]
    losses = [p for p in net_pnls if p < 0]
    win_rate = len(wins) / total * 100 if total else 0.0
    gross_w  = sum(wins)
    gross_l  = abs(sum(losses))
    pf       = gross_w / gross_l if gross_l else 0.0
    net_profit = sum(net_pnls)

    # Max drawdown
    equity, peak, max_dd = 0.0, 0.0, 0.0
    equity_curve = []
    for date_str in sorted(daily_pnl):
        equity += daily_pnl[date_str]
        if equity > peak: peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0
        if dd > max_dd: max_dd = dd
        equity_curve.append({"date": date_str, "value": round(equity, 2)})

    # Sharpe ratio
    daily_vals = list(daily_pnl.values())
    if len(daily_vals) > 1:
        mean_r = sum(daily_vals) / len(daily_vals)
        var    = sum((r - mean_r) ** 2 for r in daily_vals) / len(daily_vals)
        std_r  = math.sqrt(var) if var > 0 else 1
        sharpe = (mean_r / std_r) * math.sqrt(252) if std_r else 0
        # Sortino (downside deviation only)
        down_dev = math.sqrt(sum(r**2 for r in daily_vals if r < 0) / len(daily_vals)) if daily_vals else 1
        sortino  = (mean_r / down_dev) * math.sqrt(252) if down_dev else 0
    else:
        sharpe = sortino = 0.0

    monthly: Dict[str, float] = {}
    for ds, pnl in daily_pnl.items():
        month = ds[:7]
        monthly[month] = monthly.get(month, 0.0) + pnl

    avg_win  = gross_w / len(wins)   if wins   else 0
    avg_loss = gross_l / len(losses) if losses else 0
    expectancy_r = (win_rate/100 * (avg_win/settings.capital.total*100)) - ((1 - win_rate/100) * (avg_loss/settings.capital.total*100))

    return {
        "metrics": {
            "total_trades":     total,
            "win_rate":         round(win_rate, 2),
            "avg_win_r":        round(gross_w / len(wins)   / max(settings.capital.total * 0.01, 1), 2) if wins   else 0,
            "avg_loss_r":       round(gross_l / len(losses) / max(settings.capital.total * 0.01, 1), 2) if losses else 0,
            "profit_factor":    round(pf, 2),
            "expectancy_r":     round(expectancy_r, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "sharpe_ratio":     round(sharpe, 2),
            "sortino_ratio":    round(sortino, 2),
            "calmar_ratio":     round((net_profit / settings.capital.total * 100) / max_dd, 2) if max_dd else 0,
            "net_profit":       round(net_profit, 2),
            "net_profit_pct":   round(net_profit / settings.capital.total * 100, 2),
            "best_day":         round(max(daily_vals), 2) if daily_vals else 0,
            "worst_day":        round(min(daily_vals), 2) if daily_vals else 0,
            "avg_daily_pnl":    round(sum(daily_vals) / len(daily_vals), 2) if daily_vals else 0,
        },
        "equity_curve": equity_curve,
        "monthly_returns": {k: round(v, 2) for k, v in sorted(monthly.items())},
        "performance": [
            {"date": d, "net_pnl": round(p, 2), "trades_count": 0, "win_rate": 0, "equity": 0}
            for d, p in sorted(daily_pnl.items())
        ],
    }
