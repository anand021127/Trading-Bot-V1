"""AI shadow-mode logging.

PHASE 9: in shadow mode the existing bot's real/paper decision is
untouched — this module only OBSERVES and records what the AI would have
done, alongside enough context to score it later against the real
outcome. No orders are placed or modified from here; this module has no
access to OrderManager at all, by design.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from backend.ai.predictor import AIDecision

DEFAULT_LOG_PATH = Path("logs/ai_shadow_log.csv")

FIELDS = [
    "timestamp", "symbol", "underlying", "strategy_signal", "strategy_confidence",
    "ai_ran", "ai_probability", "ai_should_allow", "ai_reason", "ai_model_version",
    "ai_mode", "market_regime", "actual_outcome", "would_have_r",
]


def log_decision(
    symbol: str,
    strategy_signal: str,
    strategy_confidence: float,
    ai_decision: AIDecision,
    market_regime: str = "",
    underlying: str = "",
    log_path: Path = DEFAULT_LOG_PATH,
) -> None:
    """Append one row. actual_outcome/would_have_r are left blank at
    decision time and filled in later by a reconciliation job once the
    trade would have closed — this function only ever appends, it never
    rewrites prior rows."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not log_path.exists()
    with open(log_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "underlying": underlying,
            "strategy_signal": strategy_signal,
            "strategy_confidence": round(strategy_confidence, 2),
            "ai_ran": ai_decision.ran,
            "ai_probability": ai_decision.probability,
            "ai_should_allow": ai_decision.should_allow,
            "ai_reason": ai_decision.reason,
            "ai_model_version": ai_decision.model_version,
            "ai_mode": ai_decision.mode,
            "market_regime": market_regime,
            "actual_outcome": "",
            "would_have_r": "",
        })
