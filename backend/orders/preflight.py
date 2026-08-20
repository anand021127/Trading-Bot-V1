"""Pre-trade safety checklist for production mode.

Before ANY order is placed in live mode, every check here must pass.
If any fails: NO TRADE, detailed log. Paper mode bypasses live-specific
checks but still validates contract/risk fundamentals.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class PreflightResult:
    """Result of a pre-trade safety check."""
    passed: bool
    checks: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    blocking_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": self.checks,
            "blocking_reasons": self.blocking_reasons,
        }


def run_preflight(
    *,
    mode: str = "paper",
    access_token: Optional[str] = None,
    ws_connected: bool = False,
    instrument_key: Optional[str] = None,
    tick_cache_has_key: bool = False,
    tick_age_seconds: Optional[float] = None,
    max_tick_age: float = 30.0,
    risk_allowed: bool = True,
    risk_reason: str = "",
    exposure_allowed: bool = True,
    exposure_reason: str = "",
    lot_risk_allowed: bool = True,
    lot_risk_reason: str = "",
    kill_switch_active: bool = False,
    reconciliation_ok: bool = True,
    reconciliation_reason: str = "",
) -> PreflightResult:
    """Run the full pre-trade safety checklist.

    In paper mode, live-feed checks are warnings rather than blockers.
    In live mode, ALL checks must pass.
    """
    is_live = mode == "live"
    checks: Dict[str, Dict[str, Any]] = {}
    blocking: List[str] = []

    # 1. Valid token
    has_token = bool(access_token and len(access_token) > 10)
    checks["valid_token"] = {"pass": has_token, "note": "Access token present and non-empty"}
    if is_live and not has_token:
        blocking.append("No valid Upstox access token for live trading")

    # 2. WebSocket connected
    checks["ws_connected"] = {"pass": ws_connected, "note": "V3 WebSocket feed is connected"}
    if is_live and not ws_connected:
        blocking.append("V3 WebSocket feed is not connected — no live price data")

    # 3. Instrument key present
    has_key = bool(instrument_key)
    checks["instrument_key"] = {"pass": has_key, "note": f"instrument_key={instrument_key}"}
    if not has_key:
        blocking.append("No instrument_key resolved for this option contract")

    # 4. Live tick received for this contract
    checks["tick_received"] = {"pass": tick_cache_has_key, "note": "Live tick in WS cache"}
    if is_live and not tick_cache_has_key:
        blocking.append(f"No live tick received for {instrument_key}")

    # 5. Tick freshness
    fresh = tick_age_seconds is not None and tick_age_seconds <= max_tick_age
    checks["tick_fresh"] = {
        "pass": fresh,
        "value": tick_age_seconds,
        "threshold": max_tick_age,
        "note": f"Tick age {tick_age_seconds}s vs max {max_tick_age}s",
    }
    if is_live and not fresh:
        blocking.append(
            f"Option tick is stale ({tick_age_seconds}s > {max_tick_age}s threshold)"
        )

    # 6. Risk limits
    checks["risk_allowed"] = {"pass": risk_allowed, "note": risk_reason or "Risk check passed"}
    if not risk_allowed:
        blocking.append(f"Risk check failed: {risk_reason}")

    # 7. Lot risk
    checks["lot_risk_ok"] = {"pass": lot_risk_allowed, "note": lot_risk_reason or "Lot risk OK"}
    if not lot_risk_allowed:
        blocking.append(f"Lot risk: {lot_risk_reason}")

    # 8. Exposure limits
    checks["exposure_ok"] = {"pass": exposure_allowed, "note": exposure_reason or "Exposure OK"}
    if not exposure_allowed:
        blocking.append(f"Exposure: {exposure_reason}")

    # 9. Position reconciliation
    checks["reconciliation_ok"] = {"pass": reconciliation_ok, "note": reconciliation_reason or "OK"}
    if is_live and not reconciliation_ok:
        blocking.append(f"Position reconciliation: {reconciliation_reason}")

    # 10. Kill switch OFF
    checks["kill_switch_off"] = {"pass": not kill_switch_active, "note": "Kill switch is OFF"}
    if kill_switch_active:
        blocking.append("Emergency kill switch is ACTIVE — all trading halted")

    passed = len(blocking) == 0
    result = PreflightResult(passed=passed, checks=checks, blocking_reasons=blocking)

    if not passed:
        logger.warning(
            "PREFLIGHT FAILED — %d blocking issues: %s",
            len(blocking), "; ".join(blocking),
        )
    else:
        logger.debug("Preflight passed — all %d checks OK", len(checks))

    return result
