"""TradePlan: the structured object the Copilot proposes, and the
deterministic validation it must pass. No LLM output ever reaches
RiskManager/PositionSizer/OrderManager directly — only a TradePlan that
`validate_trade_plan()` has approved, and even then the EXISTING
RiskManager.can_take_trade() / PositionSizer are still the final gate at
execution time. This module can reject a trade; it can never approve one
into an actual order — that stays entirely in the existing engine.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.copilot.config import CopilotSettings, load_copilot_settings


@dataclass
class TradePlan:
    symbol: str
    underlying: str
    option_type: str          # "CE" | "PE"
    strike: Optional[float]
    expiry: Optional[str]
    entry_price_low: Optional[float]  # option premium entry range (not underlying)
    entry_price_high: Optional[float]
    stop_loss: Optional[float]        # option premium stop-loss
    target_1: Optional[float]         # option premium target
    instrument_key: Optional[str] = None      # real Upstox instrument key for the selected contract
    target_2: Optional[float] = None
    quantity: Optional[int] = None    # from the existing PositionSizer, lot-rounded — never guessed
    lot_size: Optional[int] = None    # real broker lot size for this contract
    freeze_quantity: Optional[int] = None  # real broker freeze/quantity-freeze limit for this contract
    trailing_stop_rule: str = ""
    risk_reward: Optional[float] = None
    reason: str = ""
    ai_confidence: Optional[float] = None   # from backend/ai/ ML layer, if available — one input, not the decision
    market_regime: str = ""
    strategy_confirmation: str = ""         # which existing strategy/signal this echoes
    open_interest: Optional[int] = None     # real OI from the option chain, for liquidity context
    bid_price: Optional[float] = None
    ask_price: Optional[float] = None
    spread_pct: Optional[float] = None
    delta: Optional[float] = None
    theta: Optional[float] = None
    iv: Optional[float] = None
    quote_timestamp: Optional[str] = None   # when the live data behind this plan was fetched
    analysis_timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationResult:
    approved: bool
    reasons_rejected: List[str] = field(default_factory=list)
    checks: Dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _quote_age_seconds(quote_timestamp: Optional[str]) -> Optional[float]:
    if not quote_timestamp:
        return None
    try:
        ts = datetime.fromisoformat(quote_timestamp)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return None


def validate_trade_plan(
    plan: TradePlan,
    risk_manager: Any,
    settings: Optional[CopilotSettings] = None,
    spread_pct: Optional[float] = None,
    max_spread_pct: float = 3.0,
) -> ValidationResult:
    """Every check here is deterministic Python — no model output is
    trusted. A single failed check rejects the whole plan; nothing here
    "mostly passes." This mirrors the reject list in the spec exactly."""
    settings = settings or load_copilot_settings()
    checks: Dict[str, bool] = {}
    reasons: List[str] = []

    def check(name: str, ok: bool, reason: str = "") -> None:
        checks[name] = ok
        if not ok:
            reasons.append(reason or name)

    # 1. Structural validity — every required price must actually be present and numeric.
    has_prices = all(v is not None for v in (plan.entry_price_low, plan.entry_price_high, plan.stop_loss, plan.target_1))
    check("prices_present", has_prices, "TradePlan is missing entry/stop/target prices — cannot validate.")
    if not has_prices:
        return ValidationResult(approved=False, reasons_rejected=reasons, checks=checks)

    entry_mid = (plan.entry_price_low + plan.entry_price_high) / 2.0

    # 2. SL direction validity (options are always long premium here — CE/PE buying)
    sl_valid = plan.stop_loss < entry_mid < plan.target_1 if plan.option_type in ("CE", "PE") else False
    check("stop_loss_valid", sl_valid, f"Stop loss {plan.stop_loss} is not below entry {entry_mid} / below target {plan.target_1}.")

    # 3. Risk/reward floor
    risk = entry_mid - plan.stop_loss if sl_valid else None
    reward = plan.target_1 - entry_mid if sl_valid else None
    rr = (reward / risk) if (risk and risk > 0) else None
    plan.risk_reward = round(rr, 3) if rr is not None else None
    check("risk_reward_ok", rr is not None and rr >= settings.min_risk_reward,
          f"Risk/reward {rr} is below the configured minimum {settings.min_risk_reward}.")

    # 4. Quote/data freshness
    age = _quote_age_seconds(plan.quote_timestamp)
    check("quote_fresh", age is not None and age <= settings.max_quote_age_seconds,
          f"Quote is {age if age is not None else 'unknown'}s old (max {settings.max_quote_age_seconds}s) — market data is stale.")

    # 5. Spread/liquidity, only checked when the caller actually supplied it —
    #    absence of spread data is itself a rejection (per "do not invent unavailable data").
    check("spread_acceptable", spread_pct is not None and spread_pct <= max_spread_pct,
          f"Spread {spread_pct if spread_pct is not None else 'unknown'}% exceeds {max_spread_pct}% or wasn't supplied.")

    # 6-9. Existing RiskManager is the single source of truth for these —
    #      not re-implemented here, just called.
    if risk_manager is None:
        check("risk_manager_available", False, "No RiskManager attached — cannot validate risk limits.")
    else:
        try:
            allowed, rm_reason = risk_manager.can_take_trade(plan.symbol)
            check("risk_manager_allows", allowed, rm_reason or "RiskManager rejected this trade.")
        except Exception as e:
            check("risk_manager_allows", False, f"RiskManager check raised an error: {e}")

        # Lot-level risk: even ONE lot might risk more than the configured
        # per-trade limit — mirrors the exact gate
        # TradingEngine.execute_multi_signal() applies before sizing (see
        # backend/strategy/trading_engine.py), not a re-implementation.
        if plan.lot_size and hasattr(risk_manager, "check_lot_risk"):
            try:
                lot_ok, lot_reason = risk_manager.check_lot_risk(
                    entry_price=entry_mid, stop_loss=plan.stop_loss, lot_size=plan.lot_size,
                )
                check("lot_risk_ok", lot_ok, lot_reason or "Lot-level risk exceeds the configured limit.")
            except Exception as e:
                check("lot_risk_ok", False, f"check_lot_risk raised an error: {e}")

    approved = all(checks.values())
    return ValidationResult(approved=approved, reasons_rejected=reasons, checks=checks)
