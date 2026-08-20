"""Position Recovery and Broker Reconciliation Module.

Handles application restarts, verifying SQLite database positions against Upstox
live positions before allowing any strategy execution. If any discrepancy is found:
- Sets trading_halted = True
- Logs critical mismatch alert
- Prevents new entries until manual/automated resolution
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class RecoveryStatus:
    reconciled: bool
    trading_halted: bool
    local_position_count: int
    broker_position_count: int
    mismatches: List[Dict[str, Any]] = field(default_factory=list)
    halt_reason: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reconciled": self.reconciled,
            "trading_halted": self.trading_halted,
            "local_position_count": self.local_position_count,
            "broker_position_count": self.broker_position_count,
            "mismatches": self.mismatches,
            "halt_reason": self.halt_reason,
            "timestamp": self.timestamp,
        }


def recover_and_reconcile_positions(
    sqlite_positions: List[Dict[str, Any]],
    broker_positions: List[Dict[str, Any]],
) -> RecoveryStatus:
    """Compare local database positions vs live broker positions.

    sqlite_positions: List of dicts representing open positions in SQLite.
    broker_positions: List of dicts returned by UpstoxClient get_positions().
    """
    mismatches = []
    
    # Filter active broker positions (net quantity != 0)
    active_broker = {
        bp.get("instrument_key"): bp
        for bp in broker_positions
        if bp.get("instrument_key") and abs(int(bp.get("quantity", 0))) > 0
    }

    active_local = {
        lp.get("contract_instrument_key") or lp.get("instrument_key") or lp.get("symbol"): lp
        for lp in sqlite_positions
        if lp.get("quantity", 0) > 0
    }

    # 1. Check local positions against broker
    for key, lp in active_local.items():
        if key not in active_broker:
            mismatches.append({
                "type": "ORPHANED_LOCAL_POSITION",
                "instrument_key": key,
                "detail": f"Position exists in database (qty={lp.get('quantity')}) but NOT at broker",
                "severity": "CRITICAL",
            })
        else:
            bp = active_broker[key]
            l_qty = int(lp.get("quantity", 0))
            b_qty = abs(int(bp.get("quantity", 0)))
            if l_qty != b_qty:
                mismatches.append({
                    "type": "QUANTITY_MISMATCH",
                    "instrument_key": key,
                    "detail": f"Database quantity ({l_qty}) differs from broker quantity ({b_qty})",
                    "severity": "CRITICAL",
                })

    # 2. Check broker positions against local
    for key, bp in active_broker.items():
        if key not in active_local:
            mismatches.append({
                "type": "ORPHANED_BROKER_POSITION",
                "instrument_key": key,
                "detail": f"Position exists at broker (qty={bp.get('quantity')}) but NOT in database",
                "severity": "CRITICAL",
            })

    if mismatches:
        reconciled = False
        trading_halted = True
        halt_reason = f"CRITICAL RECONCILIATION MISMATCH: {len(mismatches)} position discrepancies found on startup."
        logger.critical(halt_reason)
    else:
        reconciled = True
        trading_halted = False
        halt_reason = ""
        logger.info("Position recovery & reconciliation SUCCESS: %d positions in sync.", len(active_local))

    return RecoveryStatus(
        reconciled=reconciled,
        trading_halted=trading_halted,
        local_position_count=len(active_local),
        broker_position_count=len(active_broker),
        mismatches=mismatches,
        halt_reason=halt_reason,
    )
