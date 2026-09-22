"""Pure position recovery / broker reconciliation helpers.

Used by unit tests and can be called from TradingEngine hydrate path.
Does not touch live brokers or require network access.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RecoveryStatus:
    reconciled: bool
    trading_halted: bool
    mismatches: List[Dict[str, Any]] = field(default_factory=list)
    reason: Optional[str] = None


def _local_key(pos: Dict[str, Any]) -> str:
    return (
        pos.get("contract_instrument_key")
        or pos.get("instrument_key")
        or pos.get("symbol")
        or ""
    )


def _broker_key(pos: Dict[str, Any]) -> str:
    return pos.get("instrument_key") or pos.get("trading_symbol") or pos.get("symbol") or ""


def recover_and_reconcile_positions(
    sqlite_positions: List[Dict[str, Any]],
    broker_positions: List[Dict[str, Any]],
) -> RecoveryStatus:
    """Compare local (SQLite) open positions against broker open positions.

    Returns a RecoveryStatus describing whether the books match and whether
    trading should be halted. No side effects — pure function for tests and
    for callers that want a structured mismatch report.
    """
    local_map: Dict[str, Dict[str, Any]] = {}
    for p in sqlite_positions or []:
        k = _local_key(p)
        if k:
            local_map[k] = p

    broker_map: Dict[str, Dict[str, Any]] = {}
    for p in broker_positions or []:
        k = _broker_key(p)
        if k and int(p.get("quantity", 0) or 0) != 0:
            broker_map[k] = p

    mismatches: List[Dict[str, Any]] = []

    # Broker positions missing locally
    for b_key, b_pos in broker_map.items():
        b_qty = int(b_pos.get("quantity", 0) or 0)
        if b_key not in local_map:
            mismatches.append({
                "type": "ORPHANED_BROKER_POSITION",
                "instrument_key": b_key,
                "broker_quantity": b_qty,
            })
        else:
            l_qty = int(local_map[b_key].get("quantity", 0) or 0)
            if l_qty != b_qty:
                mismatches.append({
                    "type": "QUANTITY_MISMATCH",
                    "instrument_key": b_key,
                    "local_quantity": l_qty,
                    "broker_quantity": b_qty,
                })

    # Local positions missing at broker
    for l_key, l_pos in local_map.items():
        if l_key not in broker_map:
            mismatches.append({
                "type": "ORPHANED_LOCAL_POSITION",
                "instrument_key": l_key,
                "local_quantity": int(l_pos.get("quantity", 0) or 0),
            })

    if mismatches:
        return RecoveryStatus(
            reconciled=False,
            trading_halted=True,
            mismatches=mismatches,
            reason="; ".join(m["type"] for m in mismatches),
        )

    return RecoveryStatus(
        reconciled=True,
        trading_halted=False,
        mismatches=[],
        reason=None,
    )
