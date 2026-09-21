from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LOG_PATH = Path("data/ai_shadow_decisions.jsonl")


def log_decision(*, symbol: str, strategy_signal: str, strategy_confidence: float, ai_decision: Any) -> None:
    logger.info(
        "AI_SHADOW symbol=%s signal=%s conf=%s allow=%s ran=%s reason=%s",
        symbol, strategy_signal, strategy_confidence,
        getattr(ai_decision, "should_allow", None),
        getattr(ai_decision, "ran", None),
        getattr(ai_decision, "reason", ""),
    )
