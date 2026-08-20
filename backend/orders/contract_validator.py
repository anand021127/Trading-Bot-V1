"""Critical Contract Validator and Execution Guardrails.

Validates all 12 hard production guardrails before ANY live or shadow order can proceed:
1. instrument_key is NSE_FO
2. underlying is NIFTY50 or BANKNIFTY
3. expiry date is valid and >= current date
4. strike is valid positive integer matching strike step
5. option_type is CE or PE
6. lot_size matches index contract specification
7. live option price is available (> 0)
8. option price is NOT the underlying spot price (rejects price corruption)
9. quote timestamp is fresh (<= max_tick_age)
10. maximum account risk <= 3.0%
11. maximum capital allocation <= 20.0%
12. broker-to-database position reconciliation confirmed
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

VALID_UNDERLYINGS = {"NIFTY50", "BANKNIFTY", "NIFTY 50", "NIFTY BANK"}
STRIKE_STEPS = {"NIFTY50": 50, "NIFTY 50": 50, "BANKNIFTY": 100, "NIFTY BANK": 100}
LOT_SIZES = {"NIFTY50": 25, "NIFTY 50": 25, "BANKNIFTY": 15, "NIFTY BANK": 15}


@dataclass
class ContractValidationResult:
    is_valid: bool
    underlying: str
    instrument_key: str
    reasons: List[str] = field(default_factory=list)
    checks: Dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "underlying": self.underlying,
            "instrument_key": self.instrument_key,
            "reasons": self.reasons,
            "checks": self.checks,
        }


def validate_option_contract(
    *,
    underlying: str,
    instrument_key: str,
    strike: float,
    option_type: str,
    expiry_date: str,
    lot_size: int,
    option_ltp: float,
    underlying_spot: float,
    quote_age_seconds: Optional[float] = 0.0,
    max_quote_age: float = 30.0,
    account_equity: float = 100000.0,
    quantity: int = 25,
    stop_loss: float = 0.0,
    reconciliation_ok: bool = True,
    kill_switch_active: bool = False,
    current_date: Optional[date] = None,
) -> ContractValidationResult:
    """Rigorous pre-trade validation covering all contract, pricing, and risk dimensions."""
    checks: Dict[str, bool] = {}
    reasons: List[str] = []
    norm_underlying = underlying.upper().strip()

    # 1. Underlying Index Check
    is_valid_underlying = norm_underlying in VALID_UNDERLYINGS
    checks["valid_underlying"] = is_valid_underlying
    if not is_valid_underlying:
        reasons.append(f"Invalid underlying '{underlying}'. Only NIFTY50 / BANKNIFTY allowed.")

    # 2. Instrument Key Format
    is_nse_fo = bool(instrument_key and "NSE_FO" in instrument_key.upper())
    checks["is_nse_fo"] = is_nse_fo
    if not is_nse_fo:
        reasons.append(f"instrument_key '{instrument_key}' is not an NSE_FO option contract.")

    # 3. Expiry Date Check
    today = current_date or date.today()
    is_valid_expiry = False
    if expiry_date:
        try:
            exp_d = date.fromisoformat(expiry_date[:10])
            is_valid_expiry = exp_d >= today
        except Exception:
            is_valid_expiry = False
    checks["valid_expiry"] = is_valid_expiry
    if not is_valid_expiry:
        reasons.append(f"Expiry date '{expiry_date}' is invalid or expired (today is {today}).")

    # 4. Strike Calculation & Step Check
    step = STRIKE_STEPS.get(norm_underlying, 50)
    is_valid_strike = strike > 0 and (int(strike) % step == 0)
    checks["valid_strike"] = is_valid_strike
    if not is_valid_strike:
        reasons.append(f"Strike {strike} is invalid or does not match index step {step}.")

    # 5. CE/PE Option Type
    is_valid_option_type = option_type.upper() in {"CE", "PE"}
    checks["valid_option_type"] = is_valid_option_type
    if not is_valid_option_type:
        reasons.append(f"Option type '{option_type}' is invalid. Must be CE or PE.")

    # 6. Lot Size Check
    expected_lot = LOT_SIZES.get(norm_underlying, 25)
    is_valid_lot_size = lot_size == expected_lot and (quantity % lot_size == 0) and quantity > 0
    checks["valid_lot_size"] = is_valid_lot_size
    if not is_valid_lot_size:
        reasons.append(f"Lot size {lot_size} or quantity {quantity} does not match exchange lot {expected_lot}.")

    # 7. Option LTP Availability
    has_valid_ltp = option_ltp > 0.0
    checks["has_valid_ltp"] = has_valid_ltp
    if not has_valid_ltp:
        reasons.append(f"Option LTP is {option_ltp} (must be > 0).")

    # 8. Spot vs Option Price Corruption Check
    # If option LTP is near spot price (e.g. within 10%), it means spot was wrongly passed as option premium
    is_not_spot_price = True
    if underlying_spot > 0 and option_ltp > 0:
        ratio = abs(option_ltp - underlying_spot) / underlying_spot
        if ratio < 0.10:  # Within 10% of index level (e.g. 24,000 index vs 24,000 option price)
            is_not_spot_price = False
            reasons.append(f"CRITICAL: Option LTP ₹{option_ltp} equals spot price ₹{underlying_spot} (data corruption).")
    checks["is_not_spot_price"] = is_not_spot_price

    # 9. Quote Freshness
    is_fresh_quote = quote_age_seconds is not None and quote_age_seconds <= max_quote_age
    checks["is_fresh_quote"] = is_fresh_quote
    if not is_fresh_quote:
        reasons.append(f"Quote is stale ({quote_age_seconds}s > max {max_quote_age}s).")

    # 10. Risk Limits (Max 3% account risk)
    per_unit_risk = abs(option_ltp - stop_loss) if stop_loss > 0 else option_ltp
    total_risk = per_unit_risk * quantity
    risk_pct = (total_risk / account_equity * 100.0) if account_equity > 0 else 100.0
    risk_ok = risk_pct <= 3.01
    checks["risk_limit_ok"] = risk_ok
    if not risk_ok:
        reasons.append(f"Trade risk ₹{total_risk:,.2f} ({risk_pct:.2f}%) exceeds max allowed 3.0% of equity ₹{account_equity:,.2f}.")

    # 11. Allocation Limits (Max 20% capital allocation)
    position_value = option_ltp * quantity
    alloc_pct = (position_value / account_equity * 100.0) if account_equity > 0 else 100.0
    alloc_ok = alloc_pct <= 20.01
    checks["allocation_limit_ok"] = alloc_ok
    if not alloc_ok:
        reasons.append(f"Position value ₹{position_value:,.2f} ({alloc_pct:.2f}%) exceeds max allowed 20.0% of equity ₹{account_equity:,.2f}.")

    # 12. System Health (Reconciliation & Kill Switch)
    checks["reconciliation_ok"] = reconciliation_ok
    if not reconciliation_ok:
        reasons.append("Broker-to-database position reconciliation failed or pending.")

    checks["kill_switch_off"] = not kill_switch_active
    if kill_switch_active:
        reasons.append("Emergency kill switch is active.")

    is_valid = len(reasons) == 0
    return ContractValidationResult(
        is_valid=is_valid,
        underlying=underlying,
        instrument_key=instrument_key,
        reasons=reasons,
        checks=checks,
    )
