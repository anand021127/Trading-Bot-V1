"""Project-grounded context layer for the Copilot.

Only QUESTION-RELEVANT context is included per request (deterministic
keyword routing + the existing conversational intent router); the whole
database/log is never dumped into a prompt. Every value passes through
backend/copilot/secret_guard.redact_value with the process's real secret
values, so tokens/keys cannot reach the model or the UI.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from backend.copilot.secret_guard import (
    collect_process_secrets,
    collect_secret_values,
    redact_value,
)

__all__ = [
    "build_backtest_context",
    "build_bot_context",
    "build_context",
    "build_trade_context",
    "collect_process_secrets",
]


def _question_is_about_trades(question: str) -> bool:
    q = (question or "").lower()
    keywords = (
        "trade", "trades", "position", "positions", "entry price", "exit price",
        "strike", "expiry", "quantity", "qty", "capital used", "pnl", "p&l",
        "rejected", "rejection", "blocked", "latest trade", "last trade",
        "trade history", "order",
    )
    return any(k in q for k in keywords)


def _question_is_about_backtest(question: str) -> bool:
    q = (question or "").lower()
    keywords = (
        "backtest", "coverage", "equity", "rejection breakdown",
        "win rate", "profit factor", "drawdown", "historical",
    )
    return any(k in q for k in keywords)


def _question_is_about_health(question: str) -> bool:
    q = (question or "").lower()
    keywords = (
        "health", "worker", "scanner", "websocket", "status", "running",
        "degraded", "mode", "strategy", "risk", "pipeline", "connected",
        "bot", "kill switch", "restart", "why didn't the bot", "why is",
        "what is the current", "is the paper worker",
    )
    return any(k in q for k in keywords)



def build_bot_context(tools: Any) -> Dict[str, Any]:
    """BotContext: mode/strategy/worker/pipeline/risk/positions/today's
    trades/recent events/scanner+websocket health — assembled only from
    tool outputs that already exist."""
    ctx: Dict[str, Any] = {}
    try:
        ctx["market_status"] = tools.get_market_status()
    except Exception as e:  # pragma: no cover
        ctx["market_status"] = {"available": False, "reason": str(e)}
    try:
        ctx["bot_health"] = tools.get_bot_health()
    except Exception as e:  # pragma: no cover
        ctx["bot_health"] = {"available": False, "reason": str(e)}
    try:
        ctx["account_risk"] = tools.get_account_risk()
    except Exception as e:  # pragma: no cover
        ctx["account_risk"] = {"available": False, "reason": str(e)}
    try:
        ctx["open_positions"] = tools.get_open_positions()
    except Exception as e:  # pragma: no cover
        ctx["open_positions"] = {"available": False, "reason": str(e)}
    try:
        ctx["daily_pnl"] = tools.get_daily_pnl()
    except Exception as e:  # pragma: no cover
        ctx["daily_pnl"] = {"available": False, "reason": str(e)}
    return ctx


def build_trade_context(tools: Any, limit: int = 5) -> Dict[str, Any]:
    """TradeContext: recent trades with full trade metadata (symbol,
    strike, expiry, entry/exit, quantity, lot size, capital, P&L,
    exit reason)."""
    try:
        recent = tools.get_recent_trades(limit=max(limit, 5))
    except Exception as e:  # pragma: no cover
        return {"recent_trades": {"available": False, "reason": str(e)}}
    return {"recent_trades": recent}


def build_backtest_context(tools: Any, question: str = "") -> Dict[str, Any]:
    """BacktestContext: latest job id/status/progress/config/strategy/date
    range/result/coverage/errors — read from the live task manager."""
    try:
        from backend.backtest.task_manager import task_manager
        latest = task_manager.get_latest_task()
        if latest is None:
            return {"backtest": {"available": False, "reason": "No backtest job has been run in this backend process yet."}}
        d = latest.to_status_dict()
        return {"backtest": {
            "available": True,
            "job_id": d.get("job_id"),
            "status": d.get("status"),
            "progress_percent": d.get("progress_percent"),
            "strategy": list(getattr(latest, "strategies", []) or []),
            "symbols": list(latest.symbols),
            "start_date": latest.start_date,
            "end_date": latest.end_date,
            "interval": latest.interval,
            "elapsed_seconds": d.get("elapsed_seconds"),
            "trades_taken": d.get("trades_taken"),
            "error": d.get("error"),
            "error_details": d.get("error_details"),
            "result_summary": {
                k: latest.result.get(k)
                for k in ("net_profit", "net_profit_pct", "winning_trades", "losing_trades",
                          "accuracy_pct", "profit_factor", "max_drawdown_pct", "total_candles_scanned",
                          "signals_generated", "rejection_reason_counts", "data_source",
                          "data_coverage_pct", "result_status", "validity_reasons")
                if isinstance(latest.result, dict) and k in latest.result
            },
        }}
    except Exception as e:
        return {"backtest": {"available": False, "reason": f"Could not read backtest state: {e}"}}


def build_context(
    question: str,
    tools: Any,
    *,
    include_bot: bool = False,
    include_trades: bool = False,
    include_backtest: bool = False,
) -> Dict[str, Any]:
    """Assemble ONLY the question-relevant context sections, then redact.

    `include_*` flags come from the caller's intent routing; each builder
    additionally deep-dives when the question names that domain.
    """
    q = question or ""
    sections: Dict[str, Any] = {}

    if include_bot or _question_is_about_health(q):
        sections.update(build_bot_context(tools))

    if include_trades or _question_is_about_trades(q):
        sections.update(build_trade_context(tools))

    if include_backtest or _question_is_about_backtest(q):
        sections.update(build_backtest_context(tools, q))

    if not sections:
        # Capability/education questions get a minimal anchor so the model
        # still knows what system it is talking about — nothing more.
        sections["mode_anchor"] = {
            "mode": os.getenv("TRADING_MODE", "paper"),
            "strategy": os.getenv("TRADING_STRATEGY", "V8_D_PULLBACK_ATM"),
        }

    secrets = collect_process_secrets()
    return redact_value(sections, collect_secret_values(secrets))
