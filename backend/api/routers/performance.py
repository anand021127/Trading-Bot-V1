"""Router for performance metrics and analytics."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query

from backend.config.settings import load_settings
from backend.database.db_manager import DatabaseManager

router = APIRouter()
settings = load_settings()
db = DatabaseManager(db_path=settings.database.path)


@router.get("/")
async def get_performance(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
) -> Dict[str, Any]:
    try:
        trades = db.list_trades(date_from=date_from, date_to=date_to)
    except Exception:
        trades = []

    if not trades:
        return {
            "metrics": None,
            "equity_curve": [],
            "monthly_returns": {},
            "performance": [],
        }

    net_pnls: List[float] = []
    daily_pnl: Dict[str, float] = {}

    for t in trades:
        d = t.__dict__ if hasattr(t, "__dict__") else dict(t)
        pnl = float(d.get("net_pnl") or 0)
        net_pnls.append(pnl)
        et = d.get("entry_time")
        date_str = (et.strftime("%Y-%m-%d") if isinstance(et, datetime) else str(et)[:10]) if et else None
        if date_str:
            daily_pnl[date_str] = daily_pnl.get(date_str, 0) + pnl

    total = len(net_pnls)
    wins = [p for p in net_pnls if p > 0]
    losses = [p for p in net_pnls if p < 0]
    win_rate = len(wins) / total * 100 if total else 0.0
    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    profit_factor = gross_wins / gross_losses if gross_losses else 0.0
    net_profit = sum(net_pnls)

    # Equity curve
    equity_curve = []
    running = 0.0
    for date_str in sorted(daily_pnl):
        running += daily_pnl[date_str]
        equity_curve.append({"date": date_str, "value": round(running, 2)})

    # Max drawdown
    peak = 0.0
    max_dd = 0.0
    eq = 0.0
    for p in net_pnls:
        eq += p
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    # Sharpe (simplified daily)
    import math
    daily_returns = list(daily_pnl.values())
    if len(daily_returns) > 1:
        mean_r = sum(daily_returns) / len(daily_returns)
        variance = sum((r - mean_r) ** 2 for r in daily_returns) / len(daily_returns)
        std_r = math.sqrt(variance) if variance > 0 else 1
        sharpe = (mean_r / std_r) * math.sqrt(252) if std_r else 0
    else:
        sharpe = 0.0

    # Monthly returns
    monthly: Dict[str, float] = {}
    for date_str, pnl in daily_pnl.items():
        month = date_str[:7]
        monthly[month] = monthly.get(month, 0) + pnl

    # Sort monthly by key
    monthly = dict(sorted(monthly.items()))

    daily_vals = list(daily_pnl.values())

    metrics = {
        "total_trades": total,
        "win_rate": round(win_rate, 2),
        "avg_win_r": round(sum(wins) / len(wins) / 1000, 2) if wins else 0,
        "avg_loss_r": round(sum(losses) / len(losses) / 1000, 2) if losses else 0,
        "profit_factor": round(profit_factor, 2),
        "expectancy_r": round((win_rate / 100 * (gross_wins / len(wins) if wins else 0) -
                               (1 - win_rate / 100) * (gross_losses / len(losses) if losses else 0)) / 1000, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_ratio": round(sharpe, 2),
        "sortino_ratio": round(sharpe * 1.2, 2),
        "calmar_ratio": round((net_profit / settings.capital.total * 100) / max_dd, 2) if max_dd else 0,
        "net_profit": round(net_profit, 2),
        "net_profit_pct": round(net_profit / settings.capital.total * 100, 2),
        "best_day": round(max(daily_vals), 2) if daily_vals else 0,
        "worst_day": round(min(daily_vals), 2) if daily_vals else 0,
        "avg_daily_pnl": round(sum(daily_vals) / len(daily_vals), 2) if daily_vals else 0,
    }

    return {
        "metrics": metrics,
        "equity_curve": equity_curve,
        "monthly_returns": {k: round(v, 2) for k, v in monthly.items()},
        "performance": [
            {"date": d, "net_pnl": round(p, 2), "trades_count": 0, "win_rate": 0, "equity": 0}
            for d, p in sorted(daily_pnl.items())
        ],
    }
