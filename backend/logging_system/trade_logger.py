"""Structured trade and system logger.

Writes JSON log lines to rotating files:
  logs/trades.log   — every trade entry/exit
  logs/strategy.log — every signal evaluation
  logs/errors.log   — all exceptions
  logs/api.log      — every Upstox API call
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


_LOGS_DIR = Path(os.getenv("LOGS_DIR", "logs"))
_LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _rotating(filename: str, level: int = logging.DEBUG) -> logging.Logger:
    name = filename.replace(".log", "")
    log = logging.getLogger(f"bot.{name}")
    if log.handlers:
        return log
    log.setLevel(level)
    handler = logging.handlers.RotatingFileHandler(
        _LOGS_DIR / filename,
        maxBytes=50 * 1024 * 1024,  # 50 MB
        backupCount=10,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)
    # Also echo to console
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(f"[%(asctime)s] [{name.upper()}] %(message)s", "%H:%M:%S"))
    log.addHandler(console)
    return log


_trade_log    = _rotating("trades.log")
_strategy_log = _rotating("strategy.log")
_error_log    = _rotating("errors.log", logging.ERROR)
_api_log      = _rotating("api.log")


def _emit(logger: logging.Logger, event: Dict[str, Any]) -> None:
    event.setdefault("ts", datetime.now(timezone.utc).isoformat())
    logger.info(json.dumps(event, default=str))


class TradeLogger:
    """Central structured logger for all bot events."""

    # ─── Trade events ─────────────────────────────────────────────────────────

    @staticmethod
    def log_entry(
        trade_id: str,
        symbol: str,
        side: str,
        quantity: int,
        entry_price: float,
        stop_loss: float,
        atr: float,
        rsi: float,
        choppiness: float,
        volume_ratio: float,
        orb_high: float,
        orb_low: float,
        trend_bias: str,
        mode: str,
        conditions: Optional[Dict[str, bool]] = None,
    ) -> None:
        _emit(_trade_log, {
            "event": "ENTRY",
            "trade_id": trade_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "r_value": round(entry_price - stop_loss, 4),
            "atr": atr,
            "rsi": rsi,
            "choppiness": choppiness,
            "volume_ratio": volume_ratio,
            "orb_high": orb_high,
            "orb_low": orb_low,
            "trend_bias": trend_bias,
            "mode": mode,
            "conditions": conditions or {},
        })

    @staticmethod
    def log_exit(
        trade_id: str,
        symbol: str,
        exit_price: float,
        exit_reason: str,
        gross_pnl: float,
        net_pnl: float,
        pnl_r: float,
        duration_min: int,
        stage: int,
        mode: str,
    ) -> None:
        _emit(_trade_log, {
            "event": "EXIT",
            "trade_id": trade_id,
            "symbol": symbol,
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "gross_pnl": gross_pnl,
            "net_pnl": net_pnl,
            "pnl_r": pnl_r,
            "duration_min": duration_min,
            "stage_at_exit": stage,
            "mode": mode,
        })

    @staticmethod
    def log_daily_summary(
        date: str,
        total_trades: int,
        wins: int,
        losses: int,
        net_pnl: float,
        mode: str,
    ) -> None:
        _emit(_trade_log, {
            "event": "DAILY_SUMMARY",
            "date": date,
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total_trades * 100, 1) if total_trades else 0,
            "net_pnl": net_pnl,
            "mode": mode,
        })

    # ─── Strategy events ──────────────────────────────────────────────────────

    @staticmethod
    def log_signal(
        symbol: str,
        signal: str,
        confidence: float,
        reasons: list,
        skipped: bool,
        skip_reason: str = "",
    ) -> None:
        _emit(_strategy_log, {
            "event": "SIGNAL",
            "symbol": symbol,
            "signal": signal,
            "confidence": round(confidence, 3),
            "reasons": reasons,
            "skipped": skipped,
            "skip_reason": skip_reason,
        })

    @staticmethod
    def log_condition(symbol: str, conditions: Dict[str, bool]) -> None:
        passed = sum(1 for v in conditions.values() if v)
        _emit(_strategy_log, {
            "event": "CONDITIONS",
            "symbol": symbol,
            "passed": passed,
            "total": len(conditions),
            "conditions": conditions,
        })

    @staticmethod
    def log_risk_event(event: str, details: str, symbol: str = "") -> None:
        _emit(_strategy_log, {
            "event": f"RISK_{event.upper()}",
            "symbol": symbol,
            "details": details,
        })

    # ─── API events ───────────────────────────────────────────────────────────

    @staticmethod
    def log_api_call(endpoint: str, status_code: int, response_ms: float) -> None:
        _emit(_api_log, {
            "event": "API_CALL",
            "endpoint": endpoint,
            "status": status_code,
            "ms": round(response_ms, 1),
        })

    @staticmethod
    def log_api_error(endpoint: str, error: str) -> None:
        _emit(_api_log, {
            "event": "API_ERROR",
            "endpoint": endpoint,
            "error": error,
        })

    # ─── Error events ─────────────────────────────────────────────────────────

    @staticmethod
    def log_error(module: str, error: Exception, context: Optional[Dict[str, Any]] = None) -> None:
        _emit(_error_log, {
            "event": "ERROR",
            "module": module,
            "error_type": type(error).__name__,
            "error": str(error),
            "context": context or {},
        })

    @staticmethod
    def log_critical(module: str, message: str) -> None:
        _emit(_error_log, {
            "event": "CRITICAL",
            "module": module,
            "message": message,
        })
