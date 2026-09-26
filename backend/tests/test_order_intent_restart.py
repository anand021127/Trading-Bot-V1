"""Restart failure injection at the order-intent layer (PHASE 5 item 9).

Proves the idempotency contract survives process death:
  * INTENT durable across restart → duplicate still blocked (no second order)
  * SUBMISSION_UNKNOWN durable across restart → still gated until reconciled
  * SUBMITTED (with broker order id) durable across restart → attribution kept
Complements the phase-1 restart suite (positions, counters, equity).
"""
from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("TRADING_BOT_OFFLINE_TESTS", "1")

from backend.database.db_manager import DatabaseManager
from backend.orders.idempotency import IdempotentOrderStore, make_signal_id


def _mk():
    tmp = tempfile.mkdtemp()
    db = DatabaseManager(db_path=os.path.join(tmp, "t.db"))
    return db, IdempotentOrderStore(db), os.path.join(tmp, "t.db")


class TestOrderIntentSurvivesRestart(unittest.TestCase):
    def test_intent_blocks_duplicate_after_restart(self):
        db, store, path = _mk()
        sid = make_signal_id(strategy="V8_D_PULLBACK_ATM", timestamp="2026-09-26T10:00:00+05:30",
                             instrument="NSE_FO|1", direction="BUY")
        store.remember_intent(sid, {"signal": "s"})
        # simulate restart: brand-new db handle + store on the same file
        db.close()
        db2 = DatabaseManager(db_path=path)
        store2 = IdempotentOrderStore(db2)
        remembered = store2.remember_intent(sid, {"signal": "s"})
        self.assertTrue(remembered["duplicate"])
        self.assertIsNotNone(store2.get(sid))

    def test_unknown_status_survives_restart_and_still_gates(self):
        db, store, path = _mk()
        sid = make_signal_id(strategy="V8_D_PULLBACK_ATM", timestamp="t2",
                             instrument="NSE_FO|2", direction="SELL")
        store.remember_intent(sid, {"signal": "s"})
        store.mark_unknown(sid)  # timeout during submit
        db.close()
        db2 = DatabaseManager(db_path=path)
        store2 = IdempotentOrderStore(db2)
        self.assertEqual(store2.get(sid)["status"], "SUBMISSION_UNKNOWN")
        # after restart, the same signal is STILL a hard duplicate
        self.assertTrue(store2.remember_intent(sid, {"signal": "s"})["duplicate"])
        # reconciliation resolves it with the real broker order id
        store2.resolve_unknown(sid, "ORD-RECONCILED", "SUBMITTED")
        self.assertEqual(store2.get(sid)["status"], "SUBMITTED")
        self.assertEqual(store2.get(sid)["broker_order_id"], "ORD-RECONCILED")

    def test_submitted_intent_attribution_survives_restart(self):
        db, store, path = _mk()
        sid = make_signal_id(strategy="V8_D_PULLBACK_ATM", timestamp="t3",
                             instrument="NSE_FO|3", direction="BUY")
        store.remember_intent(sid, {"signal": "s"})
        store.mark_submitted(sid, "ORD-42")
        db.close()
        db2 = DatabaseManager(db_path=path)
        store2 = IdempotentOrderStore(db2)
        intent = store2.get(sid)
        self.assertEqual(intent["status"], "SUBMITTED")
        self.assertEqual(intent["broker_order_id"], "ORD-42")
        # broker order id attribution is monotonic — cannot be cleared
        store2.db.update_order_intent(sid, broker_order_id=None, status="SUBMITTED")
        self.assertEqual(store2.get(sid)["broker_order_id"], "ORD-42")


if __name__ == "__main__":
    unittest.main()
