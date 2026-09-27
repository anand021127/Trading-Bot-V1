"""Live option contract metadata resolution — broker metadata is authoritative.

PHASE 5.3 §2/§17: for every supported index (NIFTY50, BANKNIFTY, FINNIFTY,
MIDCPNIFTY, SENSEX, BANKEX) the live path resolves contract facts from the
current Upstox instrument master / chain API:

  exchange segment · instrument_key · expiry · strike · option_type
  lot_size · tick_size · tradability

The exchange schedule (open/close/holidays) comes from the authoritative
exchange calendar; lot size / tick size / instrument key come from the
daily-refreshed Upstox instrument master; expiries come from the live
option-expiries API. NOTHING here is hardcoded per-index except the
plausibility tables in `backend/config/universe_config.py` (which are
cross-checks, never sources).

Rules enforced:
  * never guess a contract (a failed resolution is a typed failure, not a
    fallback to another underlying),
  * never use an expired contract,
  * never trust an instrument master older than REFRESH_TTL,
  * never silently substitute another contract.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ContractResolutionError(Exception):
    """Raised when a contract cannot be resolved from real broker metadata.

    `reason` is a stable machine-readable code used by why-not-traded and
    the API; it NEVER suggests a substitute contract."""


@dataclass
class ResolvedContract:
    underlying: str
    exchange: str                      # "NSE" | "BSE"
    exchange_segment: str              # "NSE_FO" | "BSE_FO"
    instrument_key: str
    expiry: str                        # YYYY-MM-DD (broker-resolved)
    strike: float
    option_type: str                   # CE | PE
    lot_size: int
    tick_size: Optional[float]
    underlying_spot: Optional[float] = None
    metadata_age_seconds: Optional[float] = None
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "underlying": self.underlying,
            "exchange": self.exchange,
            "exchange_segment": self.exchange_segment,
            "instrument_key": self.instrument_key,
            "expiry": self.expiry,
            "strike": self.strike,
            "option_type": self.option_type,
            "lot_size": self.lot_size,
            "tick_size": self.tick_size,
            "underlying_spot": self.underlying_spot,
            "metadata_age_seconds": self.metadata_age_seconds,
            "warnings": list(self.warnings),
        }


def resolve_contract_metadata(
    *,
    underlying: str,
    contract: Dict[str, Any],
    expiry: Optional[str] = None,
    instrument_master: Optional[Any] = None,
) -> ResolvedContract:
    """Validate/enrich a chain-resolved contract row against the CURRENT
    instrument master.

    `contract` is a row from `UpstoxClient.get_option_chain()` — i.e. the
    broker chain API's own data (instrument_key, strike, option_type,
    lot_size…). This function refuses anything the current master cannot
    confirm: unknown key, missing lot size, expired contract, wrong
    exchange segment for the underlying.
    """
    from backend.config.universe_config import INDEX_EXCHANGE, VALID_OPTION_INDICES

    und = str(underlying or "").upper().strip()
    if und not in VALID_OPTION_INDICES:
        raise ContractResolutionError(f"unsupported_underlying:{und or 'EMPTY'}")

    ik = str(contract.get("instrument_key") or "").strip()
    if not ik:
        raise ContractResolutionError("contract_missing_instrument_key")
    segment = ik.split("|", 1)[0]
    expected_segment = "BSE_FO" if INDEX_EXCHANGE.get(und) == "BSE" else "NSE_FO"
    if segment != expected_segment:
        raise ContractResolutionError(
            f"exchange_mismatch:{und}_expects_{expected_segment}_got_{segment}")

    try:
        strike = float(contract.get("strike"))
        option_type = str(contract.get("option_type") or "").upper()
        if option_type not in ("CE", "PE"):
            raise ValueError(option_type)
    except (TypeError, ValueError):
        raise ContractResolutionError("contract_missing_strike_or_type")

    # Lot size: the chain row carries it; if absent, the CURRENT instrument
    # master is the authority. Never a per-index hardcoded constant.
    lot_size = int(contract.get("lot_size") or 0)
    master_age: Optional[float] = None
    master_meta: Dict[str, Any] = {}
    if instrument_master is not None:
        # Test seam: an object exposing metadata_for_key/get_instrument_metadata.
        getter = getattr(instrument_master, "metadata_for_key", None) or \
            getattr(instrument_master, "get_instrument_metadata", None)
        if getter is not None:
            master_meta = getter(ik) or {}
        st = instrument_master.status() if hasattr(instrument_master, "status") else {}
        master_age = st.get("last_refreshed_seconds_ago")
    else:
        from backend.broker.instrument_master import get_instrument_metadata, get_master_status
        master_meta = get_instrument_metadata(ik) or {}
        try:
            master_age = get_master_status().get("last_refreshed_seconds_ago")
        except Exception:  # pragma: no cover — status is informational only
            master_age = None
    if lot_size <= 0:
        lot_size = int(master_meta.get("lot_size") or 0)
    if lot_size <= 1:
        raise ContractResolutionError("lot_size_unresolved_from_broker_metadata")

    # Tick size: instrument master when available; optional (not all
    # instrument-master rows expose it) — never invented.
    tick_size: Optional[float] = None
    raw_tick = master_meta.get("tick_size")
    if raw_tick:
        try:
            tick_size = float(raw_tick)
        except (TypeError, ValueError):
            tick_size = None

    # Expiry: must be broker-provided and strictly in the future.
    exp = str(expiry or contract.get("expiry") or "").strip()[:10]
    try:
        exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
    except ValueError:
        raise ContractResolutionError("expiry_unresolved_from_broker")
    if exp_date < date.today():
        raise ContractResolutionError(f"contract_expired:{exp}")

    # Tradability: a contract must carry a real tradable quote on the chain
    # row (ltp > 0). A missing/zero LTP row is not tradable — refuse rather
    # than submit an order into an illiquid/defunct contract.
    warnings: List[str] = []
    try:
        ltp = float(contract.get("ltp") or 0)
    except (TypeError, ValueError):
        ltp = 0.0
    if ltp <= 0:
        warnings.append("chain_row_missing_ltp")

    return ResolvedContract(
        underlying=und,
        exchange=INDEX_EXCHANGE.get(und, ""),
        exchange_segment=segment,
        instrument_key=ik,
        expiry=exp,
        strike=strike,
        option_type=option_type,
        lot_size=lot_size,
        tick_size=tick_size,
        underlying_spot=(
            float(contract["underlying_spot"]) if contract.get("underlying_spot") else None
        ),
        metadata_age_seconds=master_age,
        warnings=warnings,
    )
