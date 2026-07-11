"""Paper-trading readiness status — computed from the real trade log.

Reads `logs/trades.log` (JSON lines written by TradeLogger) and pairs
ENTRY/EXIT events by trade_id. Every number here comes from that real
history — if there isn't enough of it yet, the checklist says so
explicitly (pass=False, value=None) rather than defaulting to a green
checkmark.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def _log_path() -> Path:
    return Path(os.getenv("LOGS_DIR", "logs")) / "trades.log"


def read_trade_events(mode_filter: Optional[str] = "paper") -> List[Dict[str, Any]]:
    path = _log_path()
    if not path.exists():
        return []
    events: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if mode_filter and event.get("mode") != mode_filter:
                continue
            events.append(event)
    return events


def pair_trades(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Match ENTRY -> EXIT by trade_id. Entries with no exit yet are
    returned with exit fields as None (still open, not a completed trade)."""
    entries: Dict[str, Dict[str, Any]] = {}
    exits: Dict[str, Dict[str, Any]] = {}
    for e in events:
        if e.get("event") == "ENTRY":
            entries[e["trade_id"]] = e
        elif e.get("event") == "EXIT":
            exits[e["trade_id"]] = e

    trades = []
    for trade_id, entry in entries.items():
        exit_event = exits.get(trade_id)
        trades.append({
            "trade_id": trade_id,
            "symbol": entry.get("symbol"),
            "entry_ts": entry.get("ts"),
            "exit_ts": exit_event.get("ts") if exit_event else None,
            "net_pnl": exit_event.get("net_pnl") if exit_event else None,
            "exit_reason": exit_event.get("exit_reason") if exit_event else None,
            "trend_bias": entry.get("trend_bias"),
            "is_closed": exit_event is not None,
        })
    trades.sort(key=lambda t: t["entry_ts"] or "")
    return trades


def compute_paper_status(days_required: int = 20) -> Dict[str, Any]:
    events = read_trade_events(mode_filter="paper")
    trades = pair_trades(events)
    closed = [t for t in trades if t["is_closed"]]

    days_active = len({t["entry_ts"][:10] for t in trades if t.get("entry_ts")})

    # ── metrics from real closed trades only ──────────────────────────────
    win_rate: Optional[float] = None
    profit_factor: Optional[float] = None
    max_drawdown: Optional[float] = None

    if closed:
        wins = [t for t in closed if (t["net_pnl"] or 0) > 0]
        losses = [t for t in closed if (t["net_pnl"] or 0) <= 0]
        win_rate = len(wins) / len(closed) * 100
        gross_win = sum(t["net_pnl"] for t in wins)
        gross_loss = abs(sum(t["net_pnl"] for t in losses))
        profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (
            999.99 if gross_win > 0 else 0.0
        )

        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in closed:
            equity += t["net_pnl"] or 0
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak * 100)
        max_drawdown = max_dd

    logs_complete = len(trades) > 0 and all(
        t["is_closed"] or t.get("entry_ts", "") >= _today_minus_days(1) for t in trades
    )

    checklist = {
        "win_rate_ok": {"value": win_rate, "pass": (win_rate or 0) > 40.0 if win_rate is not None else False},
        "profit_factor_ok": {"value": profit_factor, "pass": (profit_factor or 0) > 1.5 if profit_factor is not None else False},
        "max_drawdown_ok": {"value": max_drawdown, "pass": (max_drawdown is not None and max_drawdown < 5.0)},
        "logs_complete": {"value": len(trades), "pass": logs_complete},
        # These three are strategy-specific filters from the original ORB
        # design. We don't have enough structured data yet to verify them
        # from the log alone — reporting that honestly rather than a fake
        # pass.
        "orb_filter_ok": {"value": None, "pass": False, "note": "Not yet measurable from trade log"},
        "choppiness_filter_ok": {"value": None, "pass": False, "note": "Not yet measurable from trade log"},
        "time_window_ok": {"value": None, "pass": False, "note": "Not yet measurable from trade log"},
    }
    is_ready = days_active >= days_required and all(c["pass"] for c in checklist.values())

    daily_history = _build_daily_history(closed)

    return {
        "days_active": days_active,
        "days_required": days_required,
        "is_ready": is_ready,
        "checklist": checklist,
        "daily_history": daily_history,
        "total_trades_logged": len(trades),
        "closed_trades": len(closed),
        "open_trades": len(trades) - len(closed),
        "data_source": "real_trade_log" if trades else "no_trades_yet",
    }


def _today_minus_days(n: int) -> str:
    from datetime import timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def _build_daily_history(closed: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_day: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for t in closed:
        day = (t["exit_ts"] or t["entry_ts"] or "")[:10]
        if day:
            by_day[day].append(t)

    history = []
    for day in sorted(by_day.keys()):
        day_trades = by_day[day]
        wins = sum(1 for t in day_trades if (t["net_pnl"] or 0) > 0)
        losses = sum(1 for t in day_trades if (t["net_pnl"] or 0) <= 0)
        net_pnl = sum(t["net_pnl"] or 0 for t in day_trades)

        equity, peak, dd = 0.0, 0.0, 0.0
        for t in day_trades:
            equity += t["net_pnl"] or 0
            peak = max(peak, equity)
            if peak > 0:
                dd = max(dd, (peak - equity) / peak * 100)

        biases = [t["trend_bias"] for t in day_trades if t.get("trend_bias")]
        trend_bias = max(set(biases), key=biases.count) if biases else "NEUTRAL"

        history.append({
            "date": day, "trades": len(day_trades), "wins": wins, "losses": losses,
            "net_pnl": round(net_pnl, 2), "drawdown": round(dd, 2), "trend_bias": trend_bias,
        })
    return history
