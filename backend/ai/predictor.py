"""Runtime AI predictor — fail-safe adapter between the existing strategy
pipeline and the AI model.

PHASE 14 SAFETY CONTRACT:
  - If AI_ENABLED=false: `evaluate()` returns a NEUTRAL decision that never
    blocks a trade. Callers that don't check .should_allow at all get
    identical behavior to before this module existed.
  - If the model is missing, corrupted, stale, or predict_proba raises for
    any reason: `evaluate()` catches it and returns a FAILSAFE decision.
    Whether FAILSAFE means "allow" or "reject" is controlled by
    AI_FAIL_OPEN (default True = allow, i.e. behave as if AI weren't
    there) — set AI_FAIL_OPEN=false to reject on any AI malfunction instead.
  - This module NEVER raises out of `evaluate()`. A bug here must not be
    able to take down live trading.
  - This module NEVER touches risk limits, stop loss, position sizing, or
    order placement. It only returns an opinion; the caller decides what
    to do with it, and the existing RiskManager/PositionSizer/OrderManager
    are completely untouched.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from backend.ai.config import AISettings, load_ai_settings
from backend.ai.features import build_feature_vector, build_feature_vector_from_signal, feature_vector_to_row
from backend.ai.registry import load_latest_model

logger = logging.getLogger(__name__)


@dataclass
class AIDecision:
    ran: bool                 # did the model actually run (vs disabled/failsafe)?
    should_allow: bool        # the AI's recommendation — caller decides whether to honor it
    probability: Optional[float]
    reason: str
    model_version: Optional[str] = None
    mode: str = "shadow"


class AIPredictor:
    """Loads the latest registered model once and reuses it. Safe to
    instantiate even with no trained model present."""

    def __init__(self, settings: Optional[AISettings] = None) -> None:
        self.settings = settings or load_ai_settings()
        self._loaded = None
        if self.settings.enabled:
            try:
                self._loaded = load_latest_model(models_dir=__import__("pathlib").Path(self.settings.models_dir))
            except Exception as e:
                logger.warning("[ai.predictor] failed to load model, running fail-safe: %s", e)
                self._loaded = None

    def evaluate(self, candle_at_decision: Dict[str, Any], setup_result: Any) -> AIDecision:
        if not self.settings.enabled:
            return AIDecision(ran=False, should_allow=True, probability=None,
                               reason="AI_ENABLED=false — pass-through", mode=self.settings.mode)

        if self._loaded is None:
            allow = self.settings.fail_open
            return AIDecision(ran=False, should_allow=allow, probability=None,
                               reason=f"No trained model available — fail_open={self.settings.fail_open}",
                               mode=self.settings.mode)

        try:
            if setup_result is None or getattr(setup_result, "direction", "NONE") not in ("CE", "PE"):
                return AIDecision(ran=False, should_allow=True, probability=None,
                                   reason="No directional setup to evaluate", mode=self.settings.mode)

            fv = build_feature_vector(candle_at_decision, setup_result)
            if fv is None:
                return AIDecision(ran=False, should_allow=True, probability=None,
                                   reason="Feature vector unavailable", mode=self.settings.mode)

            row = feature_vector_to_row(fv)
            model = self._loaded["model"]
            proba = float(model.predict_proba([row])[0, 1])

            if not (0.0 <= proba <= 1.0):
                raise ValueError(f"Model returned invalid probability: {proba}")

            allow = proba >= self.settings.min_trade_probability
            version = self._loaded["metadata"].get("model_version")
            return AIDecision(
                ran=True, should_allow=allow, probability=round(proba, 4),
                reason=f"AI trade-success probability {proba:.2f} vs threshold {self.settings.min_trade_probability:.2f}",
                model_version=version, mode=self.settings.mode,
            )
        except Exception as e:
            logger.warning("[ai.predictor] inference failed, running fail-safe: %s", e)
            allow = self.settings.fail_open
            return AIDecision(ran=False, should_allow=allow, probability=None,
                               reason=f"AI inference error — fail_open={self.settings.fail_open}: {e}",
                               mode=self.settings.mode)

    def evaluate_signal(self, candle_at_decision: Dict[str, Any], signal: Any) -> AIDecision:
        """Same contract as `evaluate()`, but takes the live/backtest
        `StrategySignal` directly — this is what `trading_engine.py` and
        `backtest/engine.py` actually call, since a `SetupScoreResult`
        isn't what flows through those pipelines."""
        if not self.settings.enabled:
            return AIDecision(ran=False, should_allow=True, probability=None,
                               reason="AI_ENABLED=false — pass-through", mode=self.settings.mode)
        if self._loaded is None:
            allow = self.settings.fail_open
            return AIDecision(ran=False, should_allow=allow, probability=None,
                               reason=f"No trained model available — fail_open={self.settings.fail_open}",
                               mode=self.settings.mode)
        try:
            fv = build_feature_vector_from_signal(candle_at_decision, signal)
            if fv is None:
                return AIDecision(ran=False, should_allow=True, probability=None,
                                   reason="No directional setup to evaluate", mode=self.settings.mode)
            row = feature_vector_to_row(fv)
            model = self._loaded["model"]
            proba = float(model.predict_proba([row])[0, 1])
            if not (0.0 <= proba <= 1.0):
                raise ValueError(f"Model returned invalid probability: {proba}")
            allow = proba >= self.settings.min_trade_probability
            version = self._loaded["metadata"].get("model_version")
            return AIDecision(
                ran=True, should_allow=allow, probability=round(proba, 4),
                reason=f"AI trade-success probability {proba:.2f} vs threshold {self.settings.min_trade_probability:.2f}",
                model_version=version, mode=self.settings.mode,
            )
        except Exception as e:
            logger.warning("[ai.predictor] inference failed, running fail-safe: %s", e)
            allow = self.settings.fail_open
            return AIDecision(ran=False, should_allow=allow, probability=None,
                               reason=f"AI inference error — fail_open={self.settings.fail_open}: {e}",
                               mode=self.settings.mode)
