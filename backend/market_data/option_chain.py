"""Normalized option-chain analytics for supported index underlyings."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from backend.config.universe_config import VALID_OPTION_INDICES

# The standard four-quadrant read of price-vs-OI direction used across
# NSE option-chain commentary.
LONG_BUILDUP = "LONG_BUILDUP"        # price up, OI up      — fresh longs
SHORT_BUILDUP = "SHORT_BUILDUP"      # price down, OI up    — fresh shorts
SHORT_COVERING = "SHORT_COVERING"    # price up, OI down    — shorts closing
LONG_UNWINDING = "LONG_UNWINDING"    # price down, OI down  — longs closing
NEUTRAL_BUILDUP = "NEUTRAL"          # insufficient/flat data to classify


def classify_buildup(price_change: Optional[float], oi_change: Optional[float]) -> str:
    """Real classification from real price/OI change — returns NEUTRAL
    (never a guess) when either input is missing or exactly flat."""
    if price_change is None or oi_change is None:
        return NEUTRAL_BUILDUP
    if price_change > 0 and oi_change > 0:
        return LONG_BUILDUP
    if price_change < 0 and oi_change > 0:
        return SHORT_BUILDUP
    if price_change > 0 and oi_change < 0:
        return SHORT_COVERING
    if price_change < 0 and oi_change < 0:
        return LONG_UNWINDING
    return NEUTRAL_BUILDUP


def _row_buildup(row: Dict[str, Any]) -> str:
    ltp, close = row.get("ltp"), row.get("close_price")
    price_change = (ltp - close) if ltp is not None and close is not None else None
    return classify_buildup(price_change, row.get("oi_change"))


@dataclass(frozen=True)
class OptionChainSummary:
    underlying: str
    expiry: str
    spot: Optional[float]
    atm_strike: Optional[float]
    pcr: Optional[float]
    max_pain: Optional[float]
    highest_call_oi: Optional[float]
    highest_put_oi: Optional[float]
    highest_call_oi_change: Optional[float]
    highest_put_oi_change: Optional[float]
    support: Optional[float]
    resistance: Optional[float]
    strike_buildups: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


def _peak_strike(rows: Iterable[Dict[str, Any]], field: str) -> Optional[float]:
    values = [row for row in rows if row.get(field) is not None and row.get("strike") is not None]
    return float(max(values, key=lambda row: float(row[field]))["strike"]) if values else None


def _max_pain(chain: List[Dict[str, Any]]) -> Optional[float]:
    strikes = sorted({float(row["strike"]) for row in chain if row.get("strike") is not None})
    if not strikes:
        return None
    losses: Dict[float, float] = {}
    for candidate in strikes:
        losses[candidate] = sum(
            (max(candidate - float(row["strike"]), 0) if row.get("option_type") == "CE"
             else max(float(row["strike"]) - candidate, 0)) * float(row.get("oi") or 0)
            for row in chain if row.get("strike") is not None
        )
    return min(losses, key=losses.get)


def summarize_chain(
    underlying: str,
    expiry: str,
    chain: List[Dict[str, Any]],
    spot: Optional[float] = None,
) -> OptionChainSummary:
    name = underlying.upper()
    if name not in VALID_OPTION_INDICES:
        raise ValueError(f"Unsupported option underlying: {underlying}")
    calls = [row for row in chain if row.get("option_type") == "CE"]
    puts = [row for row in chain if row.get("option_type") == "PE"]
    call_oi = sum(float(row.get("oi") or 0) for row in calls)
    put_oi = sum(float(row.get("oi") or 0) for row in puts)
    strikes = [float(row["strike"]) for row in chain if row.get("strike") is not None]
    atm = min(strikes, key=lambda strike: abs(strike - spot)) if spot is not None and strikes else None

    by_strike: Dict[float, Dict[str, Any]] = {}
    for row in chain:
        strike = row.get("strike")
        if strike is None:
            continue
        entry = by_strike.setdefault(float(strike), {"strike": float(strike)})
        prefix = "call" if row.get("option_type") == "CE" else "put"
        entry[f"{prefix}_buildup"] = _row_buildup(row)
        entry[f"{prefix}_oi"] = row.get("oi")
        entry[f"{prefix}_oi_change"] = row.get("oi_change")
    strike_buildups = [by_strike[s] for s in sorted(by_strike)]

    return OptionChainSummary(
        underlying=name,
        expiry=expiry,
        spot=spot,
        atm_strike=atm,
        pcr=put_oi / call_oi if call_oi else None,
        max_pain=_max_pain(chain),
        highest_call_oi=_peak_strike(calls, "oi"),
        highest_put_oi=_peak_strike(puts, "oi"),
        highest_call_oi_change=_peak_strike(calls, "oi_change"),
        highest_put_oi_change=_peak_strike(puts, "oi_change"),
        support=_peak_strike(puts, "oi"),
        resistance=_peak_strike(calls, "oi"),
        strike_buildups=strike_buildups,
    )
