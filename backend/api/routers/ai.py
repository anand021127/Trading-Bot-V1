"""AI layer status endpoint — read-only, used by the frontend AI panel.

Deliberately has no POST/write endpoints: enabling AI, switching modes, or
loading a different model is a config/env change + restart, not a runtime
API toggle, so this can't be flipped to "live" from the UI by accident.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter

from backend.ai.config import load_ai_settings
from backend.ai.registry import load_latest_model
from backend.ai.shadow_logger import DEFAULT_LOG_PATH

router = APIRouter()


def _recent_shadow_rows(limit: int = 20) -> List[Dict[str, Any]]:
    if not DEFAULT_LOG_PATH.exists():
        return []
    try:
        with open(DEFAULT_LOG_PATH) as f:
            rows = list(csv.DictReader(f))
        return rows[-limit:]
    except Exception:
        return []


@router.get("/status")
def get_ai_status() -> Dict[str, Any]:
    settings = load_ai_settings()
    loaded = load_latest_model(models_dir=Path(settings.models_dir)) if settings.enabled else None
    metadata = loaded["metadata"] if loaded else None

    return {
        "ai_enabled": settings.enabled,
        "ai_mode": settings.mode,
        "min_confidence": settings.min_confidence,
        "min_trade_probability": settings.min_trade_probability,
        "fail_open": settings.fail_open,
        "model": {
            "loaded": loaded is not None,
            "version": metadata.get("model_version") if metadata else None,
            "model_type": metadata.get("model_type") if metadata else None,
            "val_auc": metadata.get("val_auc") if metadata else None,
            "created_at": metadata.get("created_at") if metadata else None,
        },
        "recent_decisions": _recent_shadow_rows(),
    }
