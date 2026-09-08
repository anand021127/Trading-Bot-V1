"""Copilot shadow-mode logging — PHASE 13 equivalent for the Copilot.
In shadow mode the Copilot proposes TradePlans and logs its full
reasoning + risk validation, but places no orders and modifies nothing.
Append-only, mirrors the pattern in backend/ai/shadow_logger.py.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_LOG_PATH = Path("logs/copilot_shadow_log.csv")

FIELDS = [
    "timestamp", "symbol", "direction", "option_type", "strike", "instrument_key",
    "entry_low", "entry_high", "stop_loss", "target_1", "target_2",
    "risk_reward", "reason", "ai_confidence", "market_regime",
    "strategy_confirmation", "validation_approved", "validation_reasons_rejected",
    "decision", "hypothetical_outcome",
]


def log_trade_plan(
    trade_plan_dict: Optional[Dict[str, Any]],
    validation_dict: Optional[Dict[str, Any]],
    decision: str,
    log_path: Path = DEFAULT_LOG_PATH,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not log_path.exists()
    tp = trade_plan_dict or {}
    val = validation_dict or {}
    with open(log_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": tp.get("symbol", ""),
            "direction": tp.get("option_type", ""),
            "option_type": tp.get("option_type", ""),
            "strike": tp.get("strike", ""),
            "instrument_key": tp.get("instrument_key", ""),
            "entry_low": tp.get("entry_price_low", ""),
            "entry_high": tp.get("entry_price_high", ""),
            "stop_loss": tp.get("stop_loss", ""),
            "target_1": tp.get("target_1", ""),
            "target_2": tp.get("target_2", ""),
            "risk_reward": tp.get("risk_reward", ""),
            "reason": tp.get("reason", ""),
            "ai_confidence": tp.get("ai_confidence", ""),
            "market_regime": tp.get("market_regime", ""),
            "strategy_confirmation": tp.get("strategy_confirmation", ""),
            "validation_approved": val.get("approved", ""),
            "validation_reasons_rejected": "; ".join(val.get("reasons_rejected", []) or []),
            "decision": decision,
            "hypothetical_outcome": "",  # filled in later by a reconciliation job, never at decision time
        })
