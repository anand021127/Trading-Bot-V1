"""Deterministic decision engine.

Per the spec: "The LLM can propose. The deterministic trading engine
validates." This module IS that deterministic engine for the Copilot's
market-analysis/trade-plan layer — it contains no LLM calls. The
conversational layer (conversational.py) calls into this and then has an
LLM (or the rule-based fallback) put the structured result into words;
it never asks an LLM to decide WAIT/SKIP/TRADE itself.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.copilot.trade_plan import TradePlan, ValidationResult, validate_trade_plan


@dataclass
class MarketAnalysis:
    symbol: str
    direction: str            # "BULLISH" | "BEARISH" | "NEUTRAL" | "UNKNOWN"
    confidence: float         # 0-100 — how strongly the evidence supports `direction`
    market_regime: str        # "TRENDING" | "RANGE" | "HIGH_VOLATILITY" | "LOW_VOLATILITY" | "UNCERTAIN"
    volatility: Optional[float]      # ATR as % of price
    momentum: Optional[float]        # RSI, existing indicator
    support: Optional[float]
    resistance: Optional[float]
    preferred_side: Optional[str]    # "CE" | "PE" | None
    setup_quality: Optional[float]   # existing ConfidenceScorer 0-100, if a signal exists
    risk_reward: Optional[float]
    decision: str              # "WAIT" | "SKIP" | "TRADE"
    decision_reason: str
    data_status: str = "UNKNOWN"     # "LIVE" | "STALE" | "UNKNOWN"
    data_age_seconds: Optional[float] = None
    candle_timestamp: Optional[str] = None
    fallback_data: bool = False      # true only if diagnostics explicitly requested stale data anyway
    score_breakdown: Dict[str, float] = field(default_factory=dict)  # transparency into the scoring, not just the verdict
    data_gaps: List[str] = field(default_factory=list)   # things this analysis could NOT verify

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _classify_regime(choppiness_index: Optional[float], atr_pct: Optional[float]) -> str:
    """Standard Choppiness Index interpretation (already computed by
    backend/indicators/choppiness.py): >=61.8 = ranging/choppy, <=38.2 =
    trending — the conventional thresholds for this specific indicator,
    not invented. Extreme volatility (ATR > 1.0% of price, a deliberately
    simple fixed threshold — this repo has no rolling ATR-percentile
    baseline available to the Copilot) overrides the chop-based label,
    since an operator deciding whether to trade cares more about "is this
    unusually violent right now" than the trend/range distinction when
    both are true at once."""
    if choppiness_index is None:
        return "UNCERTAIN"
    if atr_pct is not None and atr_pct >= 1.0:
        return "HIGH_VOLATILITY"
    if atr_pct is not None and atr_pct <= 0.15:
        return "LOW_VOLATILITY"
    if choppiness_index >= 61.8:
        return "RANGE"
    if choppiness_index <= 38.2:
        return "TRENDING"
    return "UNCERTAIN"


def _score_direction(ind: Dict[str, Any]) -> Dict[str, float]:
    """PHASE 3 structured scoring — trend + momentum, each a weighted sum
    of independent factors, not a single indicator forcing a verdict.
    Returns a breakdown dict so the reasoning is inspectable, not just a
    final number."""
    close, ema20, ema50, vwap, rsi = ind.get("last_close"), ind.get("ema20"), ind.get("ema50"), ind.get("vwap"), ind.get("rsi")
    breakdown: Dict[str, float] = {}

    trend_points = 0.0
    trend_max = 0.0
    for label, cond_available, is_bullish in [
        ("close_vs_ema20", close is not None and ema20 is not None, close is not None and ema20 is not None and close > ema20),
        ("close_vs_ema50", close is not None and ema50 is not None, close is not None and ema50 is not None and close > ema50),
        ("ema20_vs_ema50", ema20 is not None and ema50 is not None, ema20 is not None and ema50 is not None and ema20 > ema50),
        ("close_vs_vwap", close is not None and vwap is not None, close is not None and vwap is not None and close > vwap),
    ]:
        if cond_available:
            trend_max += 25.0
            trend_points += 25.0 if is_bullish else -25.0
    breakdown["trend_score"] = trend_points  # -100..+100, 0 if nothing was available

    momentum_score = 0.0
    if rsi is not None:
        momentum_score = max(-100.0, min(100.0, (rsi - 50.0) * 2.5))
    breakdown["momentum_score"] = momentum_score

    # Trend carries more weight than momentum (momentum alone flips too
    # easily on noise) — 60/40, and only actually usable if at least the
    # trend factors had SOME data.
    combined = 0.6 * breakdown["trend_score"] + 0.4 * breakdown["momentum_score"] if trend_max > 0 else momentum_score
    breakdown["combined_score"] = combined
    return breakdown


def build_market_analysis(
    tools: Any, symbol: str, candles: List[Dict[str, Any]],
    data_status: str = "UNKNOWN", data_age_seconds: Optional[float] = None, candle_timestamp: Optional[str] = None,
) -> MarketAnalysis:
    data_gaps: List[str] = []

    ind = tools.get_indicators(symbol, candles)
    if not ind.get("available"):
        data_gaps.append(f"indicators: {ind.get('reason')}")
        ind = {}

    data_status, data_age, candle_ts = data_status, data_age_seconds, candle_timestamp

    sr = tools.get_support_resistance(candles)
    if not sr.get("available"):
        data_gaps.append(f"support/resistance: {sr.get('reason')}")
        sr = {}

    sig = tools.get_strategy_signals(symbol, candles)
    signal_dict = None
    if sig.get("available") and sig.get("signal"):
        signal_dict = sig["signal"]
    elif not sig.get("available"):
        data_gaps.append(f"strategy_signals: {sig.get('reason')}")

    close = ind.get("last_close")
    volatility = ind.get("atr") / close * 100.0 if (ind.get("atr") and close) else None
    regime = _classify_regime(ind.get("choppiness_index"), volatility)
    momentum = ind.get("rsi")

    breakdown = _score_direction(ind) if ind else {}
    combined = breakdown.get("combined_score", 0.0)
    confidence = round(min(100.0, abs(combined)), 1)

    # Do NOT force a direction on weak/conflicting evidence — this is the
    # exact requirement from Phase 3: "If indicators conflict, return
    # NEUTRAL or UNCERTAIN rather than forcing a trade direction."
    MIN_DIRECTIONAL_CONFIDENCE = 20.0
    if not ind:
        direction = "UNKNOWN"
    elif confidence < MIN_DIRECTIONAL_CONFIDENCE:
        direction = "NEUTRAL"
    else:
        direction = "BULLISH" if combined > 0 else "BEARISH"

    preferred_side = None
    setup_quality = None
    risk_reward = None
    if signal_dict:
        preferred_side = (signal_dict.get("indicators", {}) or {}).get("option_type") \
            or (signal_dict.get("indicators", {}) or {}).get("directional_intent")
        setup_quality = signal_dict.get("setup_score") or signal_dict.get("confidence")

    # ── Decision (deterministic — no model in the loop) ──────────────
    # PHASE 2/14: stale data is a hard block — checked FIRST, before any
    # other reasoning, and cannot be overridden by a strong signal.
    if data_status == "STALE":
        decision = "SKIP"
        decision_reason = (f"Indicator data is stale (age={data_age:.0f}s if known) — trade decision blocked. "
                            f"Data as of {candle_ts}." if data_age is not None else
                            f"Indicator data is stale (age unknown) — trade decision blocked. Data as of {candle_ts}.")
    elif not candles or not ind:
        decision, decision_reason = "WAIT", "Insufficient data to analyze this symbol right now."
    elif not signal_dict:
        decision, decision_reason = "SKIP", "No qualifying strategy setup on the existing strategy engine right now."
    elif setup_quality is not None and setup_quality < 70:
        decision, decision_reason = "SKIP", f"Setup exists but confidence ({setup_quality}) is below the tradeable floor (70)."
    else:
        decision, decision_reason = "WAIT", "A qualifying setup exists — building a TradePlan for risk validation."

    return MarketAnalysis(
        symbol=symbol, direction=direction, confidence=confidence, market_regime=regime,
        volatility=round(volatility, 3) if volatility is not None else None,
        momentum=momentum, support=sr.get("support"), resistance=sr.get("resistance"),
        preferred_side=preferred_side, setup_quality=setup_quality, risk_reward=risk_reward,
        decision=decision, decision_reason=decision_reason,
        data_status=data_status, data_age_seconds=data_age, candle_timestamp=candle_ts,
        score_breakdown=breakdown, data_gaps=data_gaps,
    )


def build_trade_plan_from_signal(
    tools: Any, symbol: str, signal: Any, analysis: Optional["MarketAnalysis"] = None,
) -> Dict[str, Any]:
    """The shared core: turns an ALREADY-COMPUTED `StrategySignal` (from
    `engine.evaluate_option_premium()`) into a TradePlan + validation.
    Split out from `build_trade_plan_for_symbol` so a caller that already
    has a fresh signal — e.g. `backend/scanner/live_scanner.py`'s
    `scan_symbol()`, which calls `evaluate_option_premium` itself every
    pass — can reuse it here instead of triggering a second live chain
    fetch for the same symbol on the same cadence."""
    result: Dict[str, Any] = {
        "available": True,
        "analysis": analysis.to_dict() if analysis else None,
        "strategy_signal": signal.to_dict() if hasattr(signal, "to_dict") else None,
    }

    if signal is None or signal.signal == "NONE" or not signal.indicators:
        result["decision"] = "SKIP"
        result["trade_plan"] = None
        result["validation"] = None
        result["reason"] = "; ".join(getattr(signal, "rejected_reasons", []) or ["No qualifying option setup right now."])
        return result

    contract = signal.indicators.get("selected_contract")
    if not contract or not contract.get("instrument_key"):
        result["decision"] = "SKIP"
        result["trade_plan"] = None
        result["validation"] = None
        result["reason"] = "Strategy produced a signal but no contract was resolved from the live option chain."
        return result

    if signal.entry_price is None or signal.stop_loss is None or signal.target is None:
        result["decision"] = "WAIT"
        result["trade_plan"] = None
        result["validation"] = None
        result["reason"] = "Contract resolved but premium ATR/entry/stop/target could not be computed (insufficient premium candle history)."
        return result

    bid, ask = contract.get("bid_price"), contract.get("ask_price")
    spread_pct = round((ask - bid) / bid * 100.0, 2) if (bid and ask and bid > 0) else None
    lot_size = int(contract.get("lot_size") or 0) or None
    freeze_qty = int(contract.get("freeze_quantity") or 0) or None

    plan = TradePlan(
        symbol=symbol, underlying=symbol, option_type=contract.get("option_type", ""),
        instrument_key=contract.get("instrument_key"),
        strike=contract.get("strike"), expiry=signal.indicators.get("expiry_date"),
        entry_price_low=round(signal.entry_price * 0.995, 2), entry_price_high=round(signal.entry_price * 1.005, 2),
        stop_loss=signal.stop_loss, target_1=signal.target,
        reason=signal.entry_reason, market_regime=(analysis.market_regime if analysis else ""),
        strategy_confirmation="OPTION_PREMIUM (engine.evaluate_option_premium — real chain + real premium candles)",
        open_interest=contract.get("oi"), bid_price=bid, ask_price=ask, spread_pct=spread_pct,
        delta=contract.get("delta"), theta=contract.get("theta"), iv=contract.get("iv"),
        lot_size=lot_size, freeze_quantity=freeze_qty,
        # This reflects when the signal was computed, not a cached/stale value.
        quote_timestamp=datetime.now(timezone.utc).isoformat(),
    )

    # Quantity: mirrors TradingEngine.execute_multi_signal()'s EXACT
    # sequence (PositionSizer.calculate -> round down to a whole lot ->
    # cap at the broker's freeze limit) rather than the raw, un-rounded
    # PositionSizer output — a non-lot-multiple quantity isn't a real
    # tradable order, so showing it as "the quantity" would be misleading.
    # Without a real lot_size from the chain, quantity is left None rather
    # than guessed.
    qty = None
    if lot_size and lot_size > 0:
        try:
            if tools.engine is not None and hasattr(tools.engine, "position_sizer"):
                raw_qty = tools.engine.position_sizer.calculate(entry_price=signal.entry_price, stop_loss_price=signal.stop_loss)
                qty = max(lot_size, (raw_qty // lot_size) * lot_size)
                if freeze_qty and qty > freeze_qty:
                    qty = max(lot_size, (freeze_qty // lot_size) * lot_size)
        except Exception:
            qty = None  # not fabricated — leave None rather than guess a quantity
    plan.quantity = qty

    # Optional: consult the existing ML layer's calibrated probability as
    # ONE input (never the decision itself) — fails silently to None if
    # unavailable, per backend/ai/predictor.py's own fail-safe contract.
    try:
        from backend.ai.predictor import AIPredictor
        ai_pred = AIPredictor()
        if ai_pred.settings.enabled:
            ai_candle = {"timestamp": plan.quote_timestamp, "close": signal.entry_price}
            ai_decision = ai_pred.evaluate_signal(ai_candle, signal)
            plan.ai_confidence = ai_decision.probability
    except Exception:
        plan.ai_confidence = None

    validation = validate_trade_plan(plan, risk_manager=tools.risk_manager, spread_pct=spread_pct)

    result["trade_plan"] = plan.to_dict()
    result["validation"] = validation.to_dict()
    result["decision"] = "TRADE" if validation.approved else "SKIP"
    if not validation.approved:
        result["reason"] = "; ".join(validation.reasons_rejected)
    return result


def build_trade_plan_for_symbol(tools: Any, symbol: str, candles: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The REAL option TradePlan pipeline, on demand: fetches a fresh
    signal itself via `engine.evaluate_option_premium(symbol)` — the exact
    same method `backend/scanner/live_scanner.py` calls per symbol. Use
    this for on-demand callers (chat, the trade-plan API route). For the
    scanner's own per-pass cadence, `live_scanner.py` calls
    `build_trade_plan_from_signal()` directly with the signal it already
    computed, avoiding a duplicate chain fetch — see the scanner's
    optional `copilot_hook` parameter.

    `candles` is accepted for backward compatibility with earlier direct
    calls but is NOT required — when omitted, market regime/direction/
    support-resistance context comes from a live underlying candle fetch
    via `tools.get_live_candles()`.
    """
    if tools.engine is None or not hasattr(tools.engine, "evaluate_option_premium"):
        return {"available": False, "reason": "No trading engine (with evaluate_option_premium) attached — cannot build a real option TradePlan."}

    # Underlying-level context (direction/regime/support-resistance) is
    # informational only here — evaluate_option_premium() does its own
    # independent trend detection internally via the real broker client.
    analysis = None
    live_status, live_age, live_ts = "UNKNOWN", None, None
    if candles is None:
        live = tools.get_live_candles(symbol)
        if live.get("available"):
            candles = live["candles"]
            live_status = live.get("data_status", "UNKNOWN")
            live_age = live.get("data_age_seconds")
            live_ts = live.get("candle_timestamp")
    if candles:
        analysis = build_market_analysis(tools, symbol, candles, data_status=live_status,
                                          data_age_seconds=live_age, candle_timestamp=live_ts)

    # PHASE 2/14 hard stop: never let stale underlying data reach a trade
    # decision, no matter what evaluate_option_premium() would otherwise
    # return. Diagnostics/explanatory callers still get the analysis
    # (fallback_data=True, clearly labeled) — only the TRADE path is blocked.
    if analysis is not None and analysis.data_status == "STALE":
        return {
            "available": True, "analysis": analysis.to_dict(), "strategy_signal": None,
            "decision": "SKIP", "trade_plan": None, "validation": None,
            "reason": analysis.decision_reason,
        }

    try:
        signal = tools.engine.evaluate_option_premium(symbol)
    except Exception as e:
        return {"available": False, "reason": f"evaluate_option_premium({symbol!r}) failed: {e}"}

    return build_trade_plan_from_signal(tools, symbol, signal, analysis)
