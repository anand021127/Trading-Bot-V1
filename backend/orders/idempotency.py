"""Persist order intents so retries cannot create a second live order.

Intent lifecycle (durable, SQLite via DatabaseManager):
  INTENT              — created just before the broker call
  SUBMITTED           — broker order id acknowledged
  SUBMISSION_UNKNOWN  — outcome unobservable (timeout/reset/5xx/…): the order
                        MAY exist. This state is a HARD duplicate gate: the
                        same signal id can never resubmit until a human or
                        the reconciler resolves it (resolve_unknown).

Failed validation never creates an intent, so it cannot poison this store.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, Optional


def make_signal_id(
    *,
    strategy: str,
    timestamp: str,
    instrument: str,
    direction: str,
    unique: str = "",
) -> str:
    raw = f"{strategy}|{timestamp}|{instrument}|{direction}|{unique}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class IdempotentOrderStore:
    def __init__(self, db: Any) -> None:
        self.db = db

    def remember_intent(self, signal_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        existing = self.db.get_order_intent(signal_id)
        if existing:
            return {"duplicate": True, "intent": existing}
        self.db.save_order_intent(signal_id, payload, status="INTENT")
        return {"duplicate": False, "intent": self.db.get_order_intent(signal_id)}

    def mark_submitted(self, signal_id: str, broker_order_id: str) -> None:
        self.db.update_order_intent(signal_id, broker_order_id=broker_order_id, status="SUBMITTED")

    def mark_unknown(self, signal_id: str) -> None:
        """Outcome unobservable: hard-gate this signal id until reconciliation."""
        self.db.update_order_intent(signal_id, status="SUBMISSION_UNKNOWN")

    def resolve_unknown(self, signal_id: str, broker_order_id: Optional[str], resolved: str) -> None:
        """Reconciliation verdict for an UNKNOWN intent: either the broker
        order id (order exists) or an explicit not-found resolution."""
        fields: Dict[str, Any] = {"status": resolved}
        if broker_order_id:
            fields["broker_order_id"] = broker_order_id
        self.db.update_order_intent(signal_id, **fields)

    def clear_intent(self, signal_id: str) -> None:
        """Remove an intent that provably never created a broker order
        (synchronous pre-acceptance rejection) so a corrected resubmission
        is a legitimate new attempt."""
        try:
            self.db._connect().execute("DELETE FROM order_intents WHERE signal_id=?", (signal_id,))
            self.db._connect().commit()
        except Exception:
            pass

    def get(self, signal_id: str) -> Optional[Dict[str, Any]]:
        return self.db.get_order_intent(signal_id)
