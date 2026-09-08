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
    market_regime: str        # "TRENDING" | "RANGING" | "UNKNOWN"
    volatility: Optional[float]      # ATR as % of price
    momentum: Optional[float]        # rate-of-change, existing indicator
    support: Optional[float]
    resistance: Optional[float]
    preferred_side: Optional[str]    # "CE" | "PE" | None
    setup_quality: Optional[float]   # existing ConfidenceScorer 0-100, if a signal exists
    risk_reward: Optional[float]
    decision: str              # "WAIT" | "SKIP" | "TRADE"
    decision_reason: str
    data_gaps: List[str] = field(default_factory=list)   # things this analysis could NOT verify

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _classify_regime(choppiness_index: Optional[float]) -> str:
    # Standard Choppiness Index interpretation (already computed by
    # backend/indicators/choppiness.py): >61.8 = ranging/choppy,
    # <38.2 = trending. This is the conventional threshold for this
    # specific indicator, not an invented rule.
    if choppiness_index is None:
        return "UNKNOWN"
    if choppiness_index >= 61.8:
        return "RANGING"
    if choppiness_index <= 38.2:
        return "TRENDING"
    return "TRANSITIONAL"


def build_market_analysis(tools: Any, symbol: str, candles: List[Dict[str, Any]]) -> MarketAnalysis:
    data_gaps: List[str] = []

    ind = tools.get_indicators(symbol, candles)
    if not ind.get("available"):
        data_gaps.append(f"indicators: {ind.get('reason')}")
        ind = {}

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

    regime = _classify_regime(ind.get("choppiness_index"))
    close = ind.get("last_close")
    ema20 = ind.get("ema20")
    direction = "UNKNOWN"
    if close is not None and ema20 is not None:
        direction = "BULLISH" if close > ema20 else ("BEARISH" if close < ema20 else "NEUTRAL")

    preferred_side = None
    setup_quality = None
    risk_reward = None
    if signal_dict:
        preferred_side = (signal_dict.get("indicators", {}) or {}).get("option_type") \
            or (signal_dict.get("indicators", {}) or {}).get("directional_intent")
        setup_quality = signal_dict.get("setup_score") or signal_dict.get("confidence")

    volatility = ind.get("atr") / close * 100.0 if (ind.get("atr") and close) else None
    momentum = ind.get("rsi")

    # ── Decision (deterministic — no model in the loop) ──────────────
    if not candles or ind == {} and sig == {}:
        decision, decision_reason = "WAIT", "Insufficient data to analyze this symbol right now."
    elif not signal_dict:
        decision, decision_reason = "SKIP", "No qualifying strategy setup on the existing strategy engine right now."
    elif setup_quality is not None and setup_quality < 70:
        decision, decision_reason = "SKIP", f"Setup exists but confidence ({setup_quality}) is below the tradeable floor (70)."
    else:
        decision, decision_reason = "WAIT", "A qualifying setup exists — building a TradePlan for risk validation."

    return MarketAnalysis(
        symbol=symbol, direction=direction, market_regime=regime,
        volatility=round(volatility, 3) if volatility is not None else None,
        momentum=momentum, support=sr.get("support"), resistance=sr.get("resistance"),
        preferred_side=preferred_side, setup_quality=setup_quality, risk_reward=risk_reward,
        decision=decision, decision_reason=decision_reason, data_gaps=data_gaps,
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
    if candles is None:
        live = tools.get_live_candles(symbol)
        if live.get("available"):
            candles = live["candles"]
    if candles:
        analysis = build_market_analysis(tools, symbol, candles)

    try:
        signal = tools.engine.evaluate_option_premium(symbol)
    except Exception as e:
        return {"available": False, "reason": f"evaluate_option_premium({symbol!r}) failed: {e}"}

    return build_trade_plan_from_signal(tools, symbol, signal, analysis)
