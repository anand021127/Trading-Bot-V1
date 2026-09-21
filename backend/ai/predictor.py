from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from backend.ai.config import load_ai_settings


@dataclass
class AIDecision:
    should_allow: bool
    ran: bool
    mode: str
    reason: str
    score: float = 0.0


class AIPredictor:
    def __init__(self) -> None:
        self.settings = load_ai_settings()
        self.model = None
        if self.settings.enabled:
            models = Path(self.settings.models_dir)
            if not models.exists():
                self.model = None

    def evaluate_signal(self, candle: Dict[str, Any], signal: Any) -> AIDecision:
        if not self.settings.enabled:
            return AIDecision(should_allow=True, ran=False, mode=self.settings.mode, reason="ai_disabled")
        if self.model is None:
            if self.settings.fail_open:
                return AIDecision(should_allow=True, ran=False, mode=self.settings.mode, reason="no_model_fail_open")
            return AIDecision(should_allow=False, ran=False, mode=self.settings.mode, reason="no_model_fail_closed")
        return AIDecision(should_allow=True, ran=True, mode=self.settings.mode, reason="passthrough")
