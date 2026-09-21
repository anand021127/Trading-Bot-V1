"""Persist order intents so retries cannot create a second live order."""
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

    def get(self, signal_id: str) -> Optional[Dict[str, Any]]:
        return self.db.get_order_intent(signal_id)
