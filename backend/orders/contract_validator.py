"""Option contract validation before any order is submitted."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Optional


@dataclass
class ValidationResult:
    is_valid: bool
    reasons: List[str] = field(default_factory=list)


_NIFTY_LOTS = {25, 50, 65, 75}


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
    quote_age_seconds: float = 0.0,
    account_equity: float = 0.0,
    quantity: int = 0,
    stop_loss: float = 0.0,
) -> ValidationResult:
    reasons: List[str] = []
    ik = str(instrument_key or "")
    if not ik or ("NSE_FO" not in ik and "BSE_FO" not in ik):
        reasons.append("instrument_key must be an NSE_FO or BSE_FO option contract")
    if option_type not in ("CE", "PE"):
        reasons.append("option_type must be CE or PE")
    if option_ltp is None or float(option_ltp) <= 0:
        reasons.append("premium must be > 0")
    if underlying_spot and abs(float(option_ltp) - float(underlying_spot)) < 1e-6:
        reasons.append("option LTP equals spot price — possible quote corruption")
    if quote_age_seconds is not None and float(quote_age_seconds) > 30:
        reasons.append("quote is stale (>30s)")
    try:
        exp = datetime.strptime(str(expiry_date)[:10], "%Y-%m-%d").date()
        if exp < date.today():
            reasons.append("expiry is in the past")
    except Exception:
        reasons.append("expiry_date is invalid")

    und = (underlying or "").upper()
    if "NIFTY" in und and "BANK" not in und and "MIDCP" not in und and "FIN" not in und:
        if strike and int(round(float(strike))) % 50 != 0:
            reasons.append("strike is not a valid NIFTY step of 50")
        # Prefer known NIFTY lots when present; still allow any positive metadata lot > 1
        if lot_size and int(lot_size) > 1 and int(lot_size) not in _NIFTY_LOTS:
            # Do not reject — exchange lot sizes change; metadata is authoritative
            pass
    if not lot_size or int(lot_size) <= 1:
        reasons.append("lot size could not be resolved from contract metadata")
    if quantity and lot_size and int(quantity) % int(lot_size) != 0:
        reasons.append("quantity is not an integer multiple of lot size")
    return ValidationResult(is_valid=len(reasons) == 0, reasons=reasons)
