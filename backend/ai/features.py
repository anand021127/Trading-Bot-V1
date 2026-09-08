"""Leakage-safe feature engineering for the AI decision-filter layer.

Every feature here is derived from:
  (a) the candle history up to and including the decision bar `t`, or
  (b) the existing `SetupScoreResult` that `ConfidenceScorer.evaluate()`
      already produced for bar `t` (itself computed only from candles[:t+1]).

Nothing in this module looks at candle[t+1:] or at any future trade
outcome. `build_feature_vector` is the single function called both by
the offline dataset builder (backend/ai/dataset.py) and by the live/
backtest runtime predictor (backend/ai/predictor.py) — using the same
function in both places is what prevents train/serve skew.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# The exact, ordered feature list the model is trained and served on.
# Changing this list requires retraining — see backend/ai/registry.py.
FEATURE_NAMES: List[str] = [
    "setup_confidence",         # ConfidenceScorer 0-100 transparency score
    "direction_is_ce",          # 1.0 = CE (bullish), 0.0 = PE (bearish)
    "factor_trend",
    "factor_momentum",
    "factor_vwap",
    "factor_volume",
    "rsi",
    "choppiness_index",
    "volume_ratio",
    "roc_1bar",
    "roc_3bar",
    "roc_5bar",
    "ema20_slope",
    "dist_ema20_atr",
    "is_overextended",
    "close_vs_vwap_pct",        # (close - vwap) / vwap * 100
    "close_vs_ema50_pct",
    "atr_pct",                  # ATR as % of price (normalized volatility)
    "minutes_since_open",       # time-of-day, session_manager owns the session clock
    "day_of_week",              # 0=Mon .. 4=Fri
    "setup_is_momentum",
    "setup_is_pullback",
    "setup_is_breakout",
]


def _minutes_since_open(ts_str: str) -> float:
    """Minutes since 09:15 IST. Returns -1.0 if the timestamp can't be parsed
    (fails safe into a neutral/out-of-range bucket rather than raising)."""
    try:
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
        ts = ts.astimezone(IST)
        open_dt = ts.replace(hour=9, minute=15, second=0, microsecond=0)
        return max(0.0, (ts - open_dt).total_seconds() / 60.0)
    except Exception:
        return -1.0


def _day_of_week(ts_str: str) -> float:
    try:
        ts = datetime.fromisoformat(ts_str)
        return float(ts.weekday())
    except Exception:
        return -1.0


def build_feature_vector(
    candle_at_decision: Dict[str, Any],
    setup_result: "Any",  # backend.strategy.confidence_scoring.SetupScoreResult
) -> Optional[Dict[str, float]]:
    """Build one feature row for the bar/setup the existing strategy just
    evaluated. Returns None if there is no directional setup to score
    (setup_result.direction == "NONE") — the AI layer has nothing to filter
    when the existing strategy itself produced no candidate trade.
    """
    if setup_result is None or setup_result.direction not in ("CE", "PE"):
        return None

    ind = setup_result.indicators or {}
    fs = setup_result.factor_scores or {}
    ts_str = candle_at_decision.get("timestamp", "")

    close = float(ind.get("spot_price", candle_at_decision.get("close", 0.0)) or 0.0)
    vwap = float(ind.get("vwap", close) or close)
    ema50 = float(ind.get("ema50", close) or close)
    atr = float(ind.get("atr", 0.0) or 0.0)

    row = {
        "setup_confidence": float(setup_result.confidence),
        "direction_is_ce": 1.0 if setup_result.direction == "CE" else 0.0,
        "factor_trend": float(fs.get("trend", 0.0)),
        "factor_momentum": float(fs.get("momentum", 0.0)),
        "factor_vwap": float(fs.get("vwap", 0.0)),
        "factor_volume": float(fs.get("volume", 0.0)),
        "rsi": float(ind.get("rsi", 50.0) or 50.0),
        "choppiness_index": float(ind.get("choppiness_index", 50.0) or 50.0),
        "volume_ratio": float(ind.get("volume_ratio", 1.0) or 1.0),
        "roc_1bar": float(ind.get("roc_1bar", 0.0) or 0.0),
        "roc_3bar": float(ind.get("roc_3bar", 0.0) or 0.0),
        "roc_5bar": float(ind.get("roc_5bar", 0.0) or 0.0),
        "ema20_slope": float(ind.get("ema20_slope", 0.0) or 0.0),
        "dist_ema20_atr": float(ind.get("dist_ema20_atr", 0.0) or 0.0),
        "is_overextended": 1.0 if ind.get("is_overextended") else 0.0,
        "close_vs_vwap_pct": ((close - vwap) / vwap * 100.0) if vwap else 0.0,
        "close_vs_ema50_pct": ((close - ema50) / ema50 * 100.0) if ema50 else 0.0,
        "atr_pct": (atr / close * 100.0) if close else 0.0,
        "minutes_since_open": _minutes_since_open(ts_str),
        "day_of_week": _day_of_week(ts_str),
        "setup_is_momentum": 1.0 if setup_result.setup_name == "MOMENTUM_CONTINUATION" else 0.0,
        "setup_is_pullback": 1.0 if setup_result.setup_name == "PULLBACK_RETEST" else 0.0,
        "setup_is_breakout": 1.0 if setup_result.setup_name == "BREAKOUT_EXPANSION" else 0.0,
    }
    return row


def build_feature_vector_from_signal(
    candle_at_decision: Dict[str, Any],
    signal: "Any",  # backend.strategy.signal.StrategySignal
) -> Optional[Dict[str, float]]:
    """Same feature schema as `build_feature_vector`, but built from a live/
    backtest `StrategySignal` instead of a raw `SetupScoreResult`.

    OptionPremiumStrategy (the only production strategy that runs
    ConfidenceScorer) copies `setup_res.indicators`, `.setup_name`, and
    `.factor_scores` onto the signal it returns (backend/strategy/
    strategies/option_premium.py), so this reads them back off `signal`
    directly — no re-scoring, no second source of truth. This is the
    function `backend/ai/predictor.py` calls at runtime; `features.py`'s
    `build_feature_vector` (SetupScoreResult-based) is what the offline
    dataset builder uses. Both fill the exact same `FEATURE_NAMES` schema.
    """
    direction = signal.indicators.get("directional_intent") or signal.indicators.get("option_type")
    if direction not in ("CE", "PE"):
        return None

    ind = signal.indicators or {}
    fs = signal.factor_scores or {}
    ts_str = candle_at_decision.get("timestamp", "")

    close = float(ind.get("spot_price", candle_at_decision.get("close", 0.0)) or 0.0)
    vwap = float(ind.get("vwap", close) or close)
    ema50 = float(ind.get("ema50", close) or close)
    atr = float(ind.get("atr", 0.0) or 0.0)

    row = {
        "setup_confidence": float(signal.confidence),
        "direction_is_ce": 1.0 if direction == "CE" else 0.0,
        "factor_trend": float(fs.get("trend", 0.0)),
        "factor_momentum": float(fs.get("momentum", 0.0)),
        "factor_vwap": float(fs.get("vwap", 0.0)),
        "factor_volume": float(fs.get("volume", 0.0)),
        "rsi": float(ind.get("rsi", 50.0) or 50.0),
        "choppiness_index": float(ind.get("choppiness_index", 50.0) or 50.0),
        "volume_ratio": float(ind.get("volume_ratio", 1.0) or 1.0),
        "roc_1bar": float(ind.get("roc_1bar", 0.0) or 0.0),
        "roc_3bar": float(ind.get("roc_3bar", 0.0) or 0.0),
        "roc_5bar": float(ind.get("roc_5bar", 0.0) or 0.0),
        "ema20_slope": float(ind.get("ema20_slope", 0.0) or 0.0),
        "dist_ema20_atr": float(ind.get("dist_ema20_atr", 0.0) or 0.0),
        "is_overextended": 1.0 if ind.get("is_overextended") else 0.0,
        "close_vs_vwap_pct": ((close - vwap) / vwap * 100.0) if vwap else 0.0,
        "close_vs_ema50_pct": ((close - ema50) / ema50 * 100.0) if ema50 else 0.0,
        "atr_pct": (atr / close * 100.0) if close else 0.0,
        "minutes_since_open": _minutes_since_open(ts_str),
        "day_of_week": _day_of_week(ts_str),
        "setup_is_momentum": 1.0 if signal.setup_name == "MOMENTUM_CONTINUATION" else 0.0,
        "setup_is_pullback": 1.0 if signal.setup_name == "PULLBACK_RETEST" else 0.0,
        "setup_is_breakout": 1.0 if signal.setup_name == "BREAKOUT_EXPANSION" else 0.0,
    }
    return row


def feature_vector_to_row(fv: Dict[str, float]) -> List[float]:
    """Order a feature dict into the exact vector the model expects."""
    return [float(fv.get(name, 0.0)) for name in FEATURE_NAMES]
