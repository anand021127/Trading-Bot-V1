from __future__ import annotations

from typing import Any, Dict, Optional

FEATURE_NAMES = (
    "direction_is_ce",
    "direction_is_pe",
    "setup_is_momentum",
    "setup_is_pullback",
    "setup_is_breakout",
    "confidence",
    "rsi",
    "atr",
    "choppiness_index",
    "volume_ratio",
    "vwap_dist",
    "roc_1bar",
    "roc_3bar",
    "roc_5bar",
    "ema20_slope",
    "dist_ema20_atr",
    "is_overextended",
    "spot",
)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v is False:
            return 0.0
        if v is True:
            return 1.0
        return float(v)
    except Exception:
        return default


def build_feature_vector(candle: Dict[str, Any], setup: Any) -> Optional[Dict[str, float]]:
    direction = getattr(setup, "direction", None) or ""
    if direction not in ("CE", "PE"):
        return None
    name = getattr(setup, "setup_name", "") or ""
    ind = getattr(setup, "indicators", {}) or {}
    return {
        "direction_is_ce": 1.0 if direction == "CE" else 0.0,
        "direction_is_pe": 1.0 if direction == "PE" else 0.0,
        "setup_is_momentum": 1.0 if name == "MOMENTUM_CONTINUATION" else 0.0,
        "setup_is_pullback": 1.0 if name == "PULLBACK_RETEST" else 0.0,
        "setup_is_breakout": 1.0 if name == "BREAKOUT_EXPANSION" else 0.0,
        "confidence": _f(getattr(setup, "confidence", 0.0)),
        "rsi": _f(ind.get("rsi")),
        "atr": _f(ind.get("atr")),
        "choppiness_index": _f(ind.get("choppiness_index")),
        "volume_ratio": _f(ind.get("volume_ratio")),
        "vwap_dist": _f(ind.get("spot_price", candle.get("close"))) - _f(ind.get("vwap")),
        "roc_1bar": _f(ind.get("roc_1bar")),
        "roc_3bar": _f(ind.get("roc_3bar")),
        "roc_5bar": _f(ind.get("roc_5bar")),
        "ema20_slope": _f(ind.get("ema20_slope")),
        "dist_ema20_atr": _f(ind.get("dist_ema20_atr")),
        "is_overextended": _f(ind.get("is_overextended")),
        "spot": _f(ind.get("spot_price", candle.get("close"))),
    }


def build_feature_vector_from_signal(candle: Dict[str, Any], signal: Any) -> Optional[Dict[str, float]]:
    direction = (signal.indicators or {}).get("directional_intent")
    if signal.signal in (None, "NONE") and direction not in ("CE", "PE"):
        return None
    if direction not in ("CE", "PE"):
        return None

    setup = type("Setup", (), {})()
    setup.direction = direction
    setup.setup_name = getattr(signal, "setup_name", "") or ""
    setup.confidence = getattr(signal, "confidence", 0.0)
    setup.indicators = signal.indicators or {}
    return build_feature_vector(candle, setup)


def feature_vector_to_row(fv: Dict[str, float]) -> list:
    return [float(fv[name]) for name in FEATURE_NAMES]
