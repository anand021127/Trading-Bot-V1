"""Deterministic broker failure-injection suite (PHASE 5 items 4, 5, 10).

Every scenario: the expected result must be SAFE. The critical proof is item
4: when a submit's outcome is unobservable (timeout/reset/5xx/DNS/malformed),
the classifier demands reconciliation and the pipeline must NEVER blindly
resubmit — a second submission is only allowed after the intent's state
proves the first never reached the broker (or after explicit reconciliation).
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from typing import Any, Dict, List
from unittest.mock import MagicMock

os.environ.setdefault("TRADING_BOT_OFFLINE_TESTS", "1")

from backend.orders.broker_responses import (
    ResponseKind,
    classify_broker_exception,
    classify_place_response,
)
from backend.orders.order_state import OrderState


class _TimeoutError(TimeoutError):
    pass


class _ConnReset(ConnectionError):
    pass


class _Broker500(Exception):
    status_code = 500


class _Broker400(Exception):
    status_code = 400


class _DNSFailure(OSError):
    pass


class TestAmbiguousResponseClassification(unittest.TestCase):
    def test_timeout_is_unknown_requires_reconciliation(self):
        r = classify_broker_exception(_TimeoutError("read timed out"))
        self.assertIs(r.kind, ResponseKind.UNKNOWN)
        self.assertTrue(r.requires_reconciliation)
        self.assertIsNone(r.order_id)

    def test_connection_reset_is_unknown(self):
        r = classify_broker_exception(_ConnReset("connection reset by peer"))
        self.assertIs(r.kind, ResponseKind.UNKNOWN)
        self.assertTrue(r.requires_reconciliation)

    def test_broker_500_is_unknown_not_rejected(self):
        r = classify_broker_exception(_Broker500("internal server error"))
        self.assertIs(r.kind, ResponseKind.UNKNOWN)
        self.assertTrue(r.requires_reconciliation)

    def test_dns_failure_is_unknown(self):
        r = classify_broker_exception(_DNSFailure("Name or service not known"))
        self.assertIs(r.kind, ResponseKind.UNKNOWN)
        self.assertTrue(r.requires_reconciliation)

    def test_broker_400_is_known_rejection(self):
        """4xx BEFORE any order id proves the broker refused — no order exists."""
        r = classify_broker_exception(_Broker400("invalid instrument"))
        self.assertIs(r.kind, ResponseKind.REJECTED_KNOWN)
        self.assertFalse(r.requires_reconciliation)
        self.assertIsNone(r.order_id)

    def test_malformed_response_is_unknown(self):
        r = classify_place_response({"status": "ok"})  # 2xx without order_id
        self.assertIs(r.kind, ResponseKind.UNKNOWN)
        self.assertTrue(r.requires_reconciliation)
        r2 = classify_place_response(None)
        self.assertIs(r2.kind, ResponseKind.UNKNOWN)
        r3 = classify_place_response("totally unexpected")
        self.assertIs(r3.kind, ResponseKind.UNKNOWN)

    def test_acked_response(self):
        r = classify_place_response({"status": "success", "data": {"order_id": "ABC123"}})
        self.assertIs(r.kind, ResponseKind.ACKED)
        self.assertEqual(r.order_id, "ABC123")
        self.assertFalse(r.requires_reconciliation)


class TestNoBlindResubmit(unittest.TestCase):
    """The pipeline-level contract: UNKNOWN ⇒ reconcile before retry.

    Modeled with the same IdempotentOrderStore the pipeline uses, plus a
    submit counter that would double-spend if the bot retried blindly.
    """

    def setUp(self):
        from backend.database.db_manager import DatabaseManager
        from backend.orders.idempotency import IdempotentOrderStore, make_signal_id
        self.tmp = tempfile.mkdtemp()
        self.db = DatabaseManager(db_path=os.path.join(self.tmp, "t.db"))
        self.store = IdempotentOrderStore(self.db)
        self.sid = make_signal_id(
            strategy="V8_D_PULLBACK_ATM", timestamp="2026-09-26T10:00:00+05:30",
            instrument="NSE_FO|12345", direction="BUY",
        )
        self.submits: List[str] = []

    def _attempt_submit(self, raise_exc: Exception = None, ack: str = None) -> Dict[str, Any]:
        """One broker attempt mirroring the pipeline's exact intent flow:
        remember → submit → mark_submitted / mark_unknown / clear_intent."""
        remembered = self.store.remember_intent(self.sid, {"signal": "payload"})
        if remembered["duplicate"]:
            return {"submitted": False, "reason": "duplicate_signal_intent"}
        self.submits.append("attempt")
        if raise_exc is not None:
            classification = classify_broker_exception(raise_exc)
            if classification.kind is ResponseKind.UNKNOWN:
                self.store.mark_unknown(self.sid)
            else:
                self.store.clear_intent(self.sid)  # proven never-created
            return {
                "submitted": False,
                "classification": classification.to_dict(),
            }
        if ack:
            self.store.mark_submitted(self.sid, ack)
            return {"submitted": True, "order_id": ack}
        return {"submitted": False, "classification": None}

    def test_timeout_then_retry_does_not_double_submit(self):
        # Attempt 1: timeout after the broker accepted (order EXISTS upstream).
        r1 = self._attempt_submit(raise_exc=_TimeoutError("timeout"))
        self.assertFalse(r1["submitted"])
        self.assertTrue(r1["classification"]["requires_reconciliation"])
        # Attempt 2 with the same signal id MUST be blocked as a duplicate —
        # reconciliation decides, never a blind resubmit.
        r2 = self._attempt_submit()
        self.assertFalse(r2["submitted"])
        self.assertEqual(r2["reason"], "duplicate_signal_intent")
        self.assertEqual(len(self.submits), 1)  # exactly ONE broker attempt

    def test_accepted_but_response_lost_then_retry_reconciles(self):
        # Attempt 1 accepted; mark_submitted persisted, but the caller crashed
        # before recording it locally. Retry with the same id is still a
        # duplicate — the intent (durable) prevents the double order.
        r1 = self._attempt_submit(ack="ORD-77")
        self.assertTrue(r1["submitted"])
        # simulate a retry of the SAME signal after a process hiccup
        r2 = self._attempt_submit()
        self.assertFalse(r2["submitted"])
        self.assertEqual(len(self.submits), 1)
        intent = self.store.get(self.sid)
        self.assertEqual(intent["broker_order_id"], "ORD-77")
        self.assertEqual(intent["status"], "SUBMITTED")

    def test_duplicate_signal_100x_never_double_submits(self):
        first = self._attempt_submit(ack="ORD-1")
        self.assertTrue(first["submitted"])
        outcomes = [self._attempt_submit() for _ in range(100)]
        self.assertTrue(all(not o["submitted"] for o in outcomes))
        self.assertEqual(len(self.submits), 1)

    def test_unknown_intent_resolved_by_reconciliation(self):
        """The full UNKNOWN lifecycle: gate → reconcile → verdict recorded."""
        r1 = self._attempt_submit(raise_exc=_TimeoutError("timeout"))
        self.assertEqual(r1["classification"]["kind"], "UNKNOWN")
        intent = self.store.get(self.sid)
        self.assertEqual(intent["status"], "SUBMISSION_UNKNOWN")
        # reconciliation finds the order DID reach the broker:
        self.store.resolve_unknown(self.sid, "ORD-FOUND-1", "SUBMITTED")
        intent = self.store.get(self.sid)
        self.assertEqual(intent["broker_order_id"], "ORD-FOUND-1")
        self.assertEqual(intent["status"], "SUBMITTED")
        # and the duplicate gate still holds — exactly one order
        r2 = self._attempt_submit()
        self.assertFalse(r2["submitted"])

    def test_known_rejection_clears_intent_for_legitimate_retry(self):
        """Failed validation / synchronous rejection does NOT poison the
        idempotency store: a corrected retry is a legitimate new attempt."""
        from backend.orders.broker_responses import classify_broker_exception
        r1 = self._attempt_submit(raise_exc=_Broker400("invalid instrument"))
        self.assertEqual(r1["classification"]["kind"], "REJECTED_KNOWN")
        # intent cleared — nothing was created at the broker
        self.assertIsNone(self.store.get(self.sid))
        # corrected retry succeeds normally
        r2 = self._attempt_submit(ack="ORD-OK")
        self.assertTrue(r2["submitted"])
        self.assertEqual(len(self.submits), 2)  # two attempts, ONE real order


class TestRiskBeforeBroker(unittest.TestCase):
    """Risk rejection must mean broker call count == 0."""

    def _pipeline(self, db, place_order_fn):
        from backend.risk.risk_config import AuthoritativeRiskConfig
        from backend.execution.pipeline import ExecutionPipeline
        risk = AuthoritativeRiskConfig(
            capital=100000.0, risk_per_trade_pct=0.01, allocation_limit_pct=0.2,
            max_daily_trades=5, max_positions=1, max_daily_loss_pct=0.05,
            lot_size_source="contract_metadata", order_product="I",
            strategy_name="V8_D_PULLBACK_ATM",
        )
        return ExecutionPipeline(
            strategy_name="V8_D_PULLBACK_ATM",
            risk=risk,
            db=db,
            place_order_fn=place_order_fn,
        )

    def _signal(self, **over):
        base = {
            "strategy": "V8_D_PULLBACK_ATM",
            "underlying": "NIFTY50",
            "instrument_key": "NSE_FO|12345",
            "strike": 24500.0,
            "option_type": "CE",
            "expiry": "2099-01-01",
            "lot_size": 25,
            "quantity": 25,
            "premium": 100.0,
            "spot": 24500.0,
            "stop_loss": 80.0,
            "quote_age_seconds": 1.0,
            "timestamp": "2026-09-26T10:00:00+05:30",
        }
        base.update(over)
        return base

    def test_max_positions_rejection_never_reaches_broker(self):
        from backend.database.db_manager import DatabaseManager
        tmp = tempfile.mkdtemp()
        db = DatabaseManager(db_path=os.path.join(tmp, "t.db"))
        spy = MagicMock(return_value=MagicMock(id="ORD-X"))
        pipe = self._pipeline(db, spy)
        result = pipe.submit_signal(self._signal())  # first ok (no state → 0 positions)
        self.assertTrue(result.accepted)
        # state says open_positions == max_positions → MAX_POSITIONS, broker untouched
        pipe2 = self._pipeline(db, spy)
        pipe2.state_provider = lambda: {"open_positions": 1}
        r2 = pipe2.submit_signal(self._signal())
        self.assertFalse(r2.accepted)
        self.assertIn("MAX_POSITIONS", r2.reason)
        self.assertEqual(spy.call_count, 1)  # only the FIRST (accepted) order hit the broker

    def test_kill_switch_blocks_submission_broker_never_called(self):
        from backend.database.db_manager import DatabaseManager
        tmp = tempfile.mkdtemp()
        db = DatabaseManager(db_path=os.path.join(tmp, "t.db"))
        spy = MagicMock(return_value=MagicMock(id="ORD-X"))
        pipe = self._pipeline(db, spy)
        pipe.kill.set_level("STOP_NEW_ENTRIES", "test")
        r = pipe.submit_signal(self._signal())
        self.assertFalse(r.accepted)
        self.assertIn("kill_switch", r.reason)
        spy.assert_not_called()

    def test_invalid_strategy_identity_rejected_before_broker(self):
        from backend.database.db_manager import DatabaseManager
        tmp = tempfile.mkdtemp()
        db = DatabaseManager(db_path=os.path.join(tmp, "t.db"))
        spy = MagicMock(return_value=MagicMock(id="ORD-X"))
        pipe = self._pipeline(db, spy)
        r = pipe.submit_signal(self._signal(strategy="OPTION_PREMIUM"))
        self.assertFalse(r.accepted)
        self.assertIn("INVALID_STRATEGY", r.reason)
        spy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
