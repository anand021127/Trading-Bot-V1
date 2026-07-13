"""Multi-strategy engine API — EMA Trend / ORB / Option Premium.

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
    symbol: str = Query(..., description="e.g. RELIANCE, HDFCBANK"),
    index_trend: Optional[str] = Query(
        None, description="BULLISH | BEARISH | NEUTRAL — for ORB's index-trend confirmation"
    ),
) -> Dict[str, Any]:
    """Run EMA_TREND + ORB against `symbol` and return every strategy's
    signal, including rejections and their reasons — nothing hidden."""
    if _engine_ref is None:
        raise HTTPException(status_code=503, detail="Trading engine not initialized (no Upstox token configured?)")

    try:
        signals = _engine_ref.evaluate_all_strategies(symbol.upper(), index_trend=index_trend)
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
    underlying: str = Query(..., description="NIFTY50 | BANKNIFTY | SENSEX"),
    expiry: Optional[str] = Query(None, description="YYYY-MM-DD — omit to auto-pick the nearest real expiry"),
    trend: Optional[str] = Query(None, description="BULLISH | BEARISH — omit to auto-detect from the underlying's own EMA trend"),
) -> Dict[str, Any]:
    if _engine_ref is None:
        raise HTTPException(status_code=503, detail="Trading engine not initialized (no Upstox token configured?)")
    if trend is not None and trend not in ("BULLISH", "BEARISH"):
        raise HTTPException(status_code=400, detail="trend must be BULLISH or BEARISH")

    try:
        signal = _engine_ref.evaluate_option_premium(underlying.upper(), expiry, trend)
    except Exception as e:
        logger.exception("Option premium evaluation failed for %s", underlying)
        raise HTTPException(status_code=502, detail=f"Option premium evaluation failed: {e}")

    return {"underlying": underlying.upper(), "signal": signal.to_dict()}
