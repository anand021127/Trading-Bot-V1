"""AI layer configuration — env-driven, deliberately defaults to OFF/SHADOW.

PHASE 8/14 requirement: the bot must behave identically to before this
package existed when AI_ENABLED=false, and must never be silently
switched to live money. Nothing here can escalate itself to "live" mode —
that requires an explicit operator-set AI_MODE=live.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _bool_env(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class AISettings:
    enabled: bool
    mode: str                    # "shadow" | "paper" | "live"
    min_confidence: float        # existing ConfidenceScorer threshold the AI layer also respects
    min_trade_probability: float  # calibrated AI probability threshold to ALLOW a trade
    fail_open: bool              # if True: AI unavailable -> fall back to existing strategy unfiltered
                                  # if False: AI unavailable -> reject the trade
    models_dir: str


def load_ai_settings() -> AISettings:
    mode = os.getenv("AI_MODE", "shadow").strip().lower()
    if mode not in ("shadow", "paper", "live"):
        mode = "shadow"  # unrecognized value fails safe to shadow, never to live

    # AI_MIN_CONFIDENCE is specified as a 0-1 fraction (per the spec example,
    # AI_MIN_CONFIDENCE=0.70) but ConfidenceScorer.confidence is on a 0-100
    # scale — convert once, here, so the rest of the codebase only ever
    # compares like-for-like scales.
    raw_min_confidence = float(os.getenv("AI_MIN_CONFIDENCE", "0.70"))
    min_confidence_0_100 = raw_min_confidence * 100.0 if raw_min_confidence <= 1.0 else raw_min_confidence

    return AISettings(
        enabled=_bool_env("AI_ENABLED", False),
        mode=mode,
        min_confidence=min_confidence_0_100,
        min_trade_probability=float(os.getenv("AI_MIN_TRADE_PROBABILITY", "0.65")),
        fail_open=_bool_env("AI_FAIL_OPEN", True),
        models_dir=os.getenv("AI_MODELS_DIR", "models/ai"),
    )
