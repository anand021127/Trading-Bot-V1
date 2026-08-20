"""Options strategy API.

Exposes the real strategy engine (backend.strategy.strategy_engine) so the
dashboard/scanner can show exactly what each strategy concluded for a
symbol, including full rejection reasons. No mock signals are ever
returned — a symbol with no data or an API failure comes back as an
explicit error, not a fabricated NONE/BUY.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query

from backend.strategy.strategy_engine import MultiStrategyEngine
from backend.config.universe_config import VALID_OPTION_INDICES

logger = logging.getLogger(__name__)
router = APIRouter()

# Shared engine reference — set by main.py at startup (same pattern as
# bot_control.py) so we reuse the same UpstoxClient/strategy instances
# rather than constructing a new one per request.
_engine_ref: Any = None
_standalone_strategy_engine = MultiStrategyEngine()  # fallback if no TradingEngine yet


def set_engine(engine: Any) -> None:
    global _engine_ref
    _engine_ref = engine


@router.get("/list")
async def list_strategies() -> Dict[str, Any]:
    engine = _engine_ref.strategy_engine if _engine_ref else _standalone_strategy_engine
    return {"strategies": engine.enabled_names()}


@router.get("/signals")
async def get_signals(
    symbol: str = Query(..., description="Supported index underlying"),
    index_trend: Optional[str] = Query(None, description="Optional underlying trend confirmation"),
) -> Dict[str, Any]:
    """Evaluate the configured options strategy against an underlying."""
    if _engine_ref is None:
        raise HTTPException(status_code=503, detail="Trading engine not initialized (no Upstox token configured?)")
    if symbol.upper() not in VALID_OPTION_INDICES:
        raise HTTPException(status_code=400, detail="Only supported index option underlyings may be evaluated")

    try:
        signals = [_engine_ref.evaluate_option_premium(symbol.upper(), underlying_trend=index_trend)]
    except Exception as e:
        logger.exception("Strategy evaluation failed for %s", symbol)
        raise HTTPException(status_code=502, detail=f"Strategy evaluation failed: {e}")

    best = MultiStrategyEngine.best_signal(signals)
    return {
        "symbol": symbol.upper(),
        "signals": [s.to_dict() for s in signals],
        "best_signal": best.to_dict() if best else None,
    }


@router.get("/option-premium")
async def get_option_premium_signal(
    underlying: str = Query(..., description="Supported index underlying"),
    expiry: Optional[str] = Query(None, description="YYYY-MM-DD — omit to auto-pick the nearest real expiry"),
    trend: Optional[str] = Query(None, description="BULLISH | BEARISH — omit to auto-detect from the underlying's own EMA trend"),
) -> Dict[str, Any]:
    if _engine_ref is None:
        raise HTTPException(status_code=503, detail="Trading engine not initialized (no Upstox token configured?)")
    if underlying.upper() not in VALID_OPTION_INDICES:
        raise HTTPException(status_code=400, detail="Unsupported index option underlying")
    if trend is not None and trend not in ("BULLISH", "BEARISH"):
        raise HTTPException(status_code=400, detail="trend must be BULLISH or BEARISH")

    try:
        signal = _engine_ref.evaluate_option_premium(underlying.upper(), expiry, trend)
    except Exception as e:
        logger.exception("Option premium evaluation failed for %s", underlying)
        raise HTTPException(status_code=502, detail=f"Option premium evaluation failed: {e}")

    return {"underlying": underlying.upper(), "signal": signal.to_dict()}
