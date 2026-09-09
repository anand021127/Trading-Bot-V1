"""Copilot configuration — env-driven, defaults to safest state.

Distinct from backend/ai/config.py: that one gates the ML probability
FILTER inside the existing strategy pipeline. This one gates the
Copilot — the conversational/decision layer described in this session's
spec. They can be enabled independently; the Copilot's TradePlan
generation optionally consults the ML layer's calibrated probability as
one input (never the sole input, per the spec).
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
class CopilotSettings:
    enabled: bool
    mode: str                  # "shadow" | "paper" | "live"
    min_risk_reward: float     # deterministic floor a TradePlan must clear
    max_quote_age_seconds: float
    llm_backend: str           # "none" | "local_openai_compatible"
    llm_base_url: str          # e.g. http://localhost:11434/v1 for Ollama
    llm_model: str
    llm_timeout_seconds: float
    max_candle_age_seconds: float = 120.0  # COPILOT_MAX_CANDLE_AGE_SECONDS — underlying/premium candle staleness limit
    gap_threshold_pct: float = 0.3  # COPILOT_GAP_THRESHOLD_PCT — |gap_percent| at/above this is GAP_UP/GAP_DOWN, else FLAT


def load_copilot_settings() -> CopilotSettings:
    mode = os.getenv("COPILOT_MODE", "shadow").strip().lower()
    if mode not in ("shadow", "paper", "live"):
        mode = "shadow"  # unrecognized -> fail safe to shadow, never to live

    backend = os.getenv("COPILOT_LLM_BACKEND", "none").strip().lower()
    if backend not in ("none", "local_openai_compatible"):
        backend = "none"  # unrecognized -> fail safe to the rule-based fallback

    return CopilotSettings(
        enabled=_bool_env("COPILOT_ENABLED", False),
        mode=mode,
        min_risk_reward=float(os.getenv("COPILOT_MIN_RISK_REWARD", "1.5")),
        max_quote_age_seconds=float(os.getenv("COPILOT_MAX_QUOTE_AGE_SECONDS", "30")),
        llm_backend=backend,
        llm_base_url=os.getenv("COPILOT_LLM_BASE_URL", "http://localhost:11434/v1"),
        llm_model=os.getenv("COPILOT_LLM_MODEL", "llama3.1:8b"),
        llm_timeout_seconds=float(os.getenv("COPILOT_LLM_TIMEOUT_SECONDS", "8")),
        max_candle_age_seconds=float(os.getenv("COPILOT_MAX_CANDLE_AGE_SECONDS", "120")),
        gap_threshold_pct=float(os.getenv("COPILOT_GAP_THRESHOLD_PCT", "0.3")),
    )
