"""Position reconciliation — compare local state vs broker positions.

Used to detect mismatches between what the bot thinks it holds and what
Upstox actually reports. Mismatches can happen due to:
  - Manual trades on the Upstox app
  - Partial fills not tracked
  - Server restarts losing in-memory state
  - Failed exit orders that weren't retried

Reconciliation result is exposed via the diagnostics API.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ReconciliationMismatch:
    instrument_key: str
    field_name: str
    local_value: Any
    broker_value: Any
    severity: str = "WARNING"  # WARNING or CRITICAL


@dataclass
class ReconciliationResult:
    matched: bool = True
    local_positions: int = 0
    broker_positions: int = 0
    mismatches: List[ReconciliationMismatch] = field(default_factory=list)
    orphaned_local: List[str] = field(default_factory=list)     # in bot, not at broker
    orphaned_broker: List[str] = field(default_factory=list)    # at broker, not in bot
    timestamp: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "matched": self.matched,
            "local_positions": self.local_positions,
            "broker_positions": self.broker_positions,
            "mismatches": [
                {
                    "instrument_key": m.instrument_key,
                    "field": m.field_name,
                    "local": m.local_value,
                    "broker": m.broker_value,
                    "severity": m.severity,
                }
                for m in self.mismatches
            ],
            "orphaned_local": self.orphaned_local,
            "orphaned_broker": self.orphaned_broker,
        }


def reconcile_positions(
    local_positions: Dict[str, Dict[str, Any]],
    broker_positions: List[Dict[str, Any]],
) -> ReconciliationResult:
    """Compare local bot positions against broker-reported positions.

    `local_positions`: The bot's _open_positions dict (keyed by symbol).
    `broker_positions`: Result of UpstoxClient.get_positions_with_details().
    """
    from datetime import datetime, timezone

    result = ReconciliationResult(
        local_positions=len(local_positions),
        broker_positions=len(broker_positions),
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    # Build broker positions index by instrument_key
    broker_by_key: Dict[str, Dict[str, Any]] = {}
    for bp in broker_positions:
        key = bp.get("instrument_key")
        if key and bp.get("quantity", 0) != 0:
            broker_by_key[key] = bp

    # Check each local position against broker
    local_keys_checked = set()
    for symbol, pos in local_positions.items():
        contract_key = pos.get("contract_instrument_key")
        if not contract_key:
            continue
        local_keys_checked.add(contract_key)

        broker_pos = broker_by_key.get(contract_key)
        if not broker_pos:
            result.orphaned_local.append(contract_key)
            result.matched = False
            result.mismatches.append(ReconciliationMismatch(
                instrument_key=contract_key,
                field_name="existence",
                local_value=f"open ({pos.get('quantity')} qty)",
                broker_value="NOT FOUND at broker",
                severity="CRITICAL",
            ))
            continue

        # Compare quantities
        local_qty = pos.get("quantity", 0)
        broker_qty = abs(broker_pos.get("quantity", 0))
        if local_qty != broker_qty:
            result.matched = False
            result.mismatches.append(ReconciliationMismatch(
                instrument_key=contract_key,
                field_name="quantity",
                local_value=local_qty,
                broker_value=broker_qty,
                severity="CRITICAL",
            ))

    # Check for broker positions not tracked locally
    for key, bp in broker_by_key.items():
        if key not in local_keys_checked:
            result.orphaned_broker.append(key)
            result.matched = False

    if not result.matched:
        logger.warning(
            "Position reconciliation MISMATCH — %d issues: %s",
            len(result.mismatches) + len(result.orphaned_broker),
            result.to_dict(),
        )
    else:
        logger.debug("Position reconciliation OK — %d positions match", result.local_positions)

    return result
