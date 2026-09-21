from __future__ import annotations

import os
from dataclasses import dataclass


def _truthy(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class AISettings:
    enabled: bool
    mode: str
    fail_open: bool
    models_dir: str


def load_ai_settings() -> AISettings:
    mode = os.environ.get("AI_MODE", "shadow").strip().lower()
    if mode not in {"shadow", "paper", "live", "off"}:
        mode = "shadow"
    if not os.environ.get("AI_MODE"):
        mode = "shadow"
    return AISettings(
        enabled=_truthy("AI_ENABLED", "false"),
        mode=mode,
        fail_open=_truthy("AI_FAIL_OPEN", "true"),
        models_dir=os.environ.get("AI_MODELS_DIR", "models/ai"),
    )
