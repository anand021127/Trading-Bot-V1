"""Order state machine + partial fill tests (PHASE 5 items 3 & 6).

Proves:
  * legal/illegal transitions for the 13-state machine
  * broker status normalization (never guesses)
  * partial fills 25+50+25 maintain filled/remaining/avg-price/capital truth
  * first partial fill is NEVER treated as complete
  * cancel-with-partial-fills keeps the fills, reflects actual quantity
  * duplicate/overfill is rejected (no duplicate quantity)
  * FILLED reported without fills → RECONCILING (never trust without evidence)
"""
from __future__ import annotations

import unittest

from backend.orders.order_state import (
    FillAggregator,
    IllegalTransitionError,
    OrderState,
    broker_state,
    can_transition,
    transition,
)


class TestLegalTransitions(unittest.TestCase):
    def test_happy_path(self):
        s = OrderState.CREATED
        for nxt in ("VALIDATING", "VALIDATED", "SUBMITTING", "SUBMITTED",
                    "PARTIALLY_FILLED", "FILLED"):
            s = transition(s, nxt)
        self.assertIs(s, OrderState.FILLED)

    def test_terminal_states_have_no_outgoing(self):
        for st in ("FILLED", "REJECTED", "CANCELLED", "FAILED"):
            for target in OrderState:
                if target.value == st:
                    continue
                self.assertFalse(can_transition(st, target), f"{st} -> {target}")

    def test_impossible_transitions_rejected(self):
        # FILLED -> CREATED is invalid
        with self.assertRaises(IllegalTransitionError):
            transition("FILLED", "CREATED")
        # REJECTED -> FILLED is invalid (same order id)
        with self.assertRaises(IllegalTransitionError):
            transition("REJECTED", "FILLED")
        # CANCELLED -> FILLED is invalid for the same order id
        with self.assertRaises(IllegalTransitionError):
            transition("CANCELLED", "FILLED")
        # CREATED -> FILLED skipping the broker entirely
        with self.assertRaises(IllegalTransitionError):
            transition("CREATED", "FILLED")
        # UNKNOWN may not be silently promoted — only reconciliation
        with self.assertRaises(IllegalTransitionError):
            transition("UNKNOWN", "FILLED")
        with self.assertRaises(IllegalTransitionError):
            transition("UNKNOWN", "REJECTED")

    def test_unknown_requires_reconciliation(self):
        self.assertTrue(can_transition("UNKNOWN", "RECONCILING"))
        self.assertTrue(can_transition("RECONCILING", "FILLED"))
        self.assertTrue(can_transition("RECONCILING", "CANCELLED"))
        self.assertTrue(can_transition("RECONCILING", "SUBMITTED"))

    def test_submitting_goes_unknown_when_response_lost(self):
        self.assertTrue(can_transition("SUBMITTING", "UNKNOWN"))


class TestBrokerNormalization(unittest.TestCase):
    def test_known_broker_strings(self):
        self.assertIs(broker_state("complete"), OrderState.FILLED)
        self.assertIs(broker_state("Completed"), OrderState.FILLED)
        self.assertIs(broker_state("open"), OrderState.SUBMITTED)
        self.assertIs(broker_state("trigger pending"), OrderState.SUBMITTED)
        self.assertIs(broker_state("rejected"), OrderState.REJECTED)
        self.assertIs(broker_state("cancelled"), OrderState.CANCELLED)
        self.assertIs(broker_state("CANCELED"), OrderState.CANCELLED)
        self.assertIs(broker_state("partial"), OrderState.PARTIALLY_FILLED)

    def test_unknown_broker_string_maps_to_unknown_never_guessed(self):
        self.assertIs(broker_state("SOMETHING_NEW"), OrderState.UNKNOWN)
        self.assertIs(broker_state(""), OrderState.UNKNOWN)
        self.assertIs(broker_state(None), OrderState.UNKNOWN)


class TestPartialFills(unittest.TestCase):
    def test_25_50_25_fills(self):
        agg = FillAggregator(requested_quantity=100)
        agg.add_fill(25, 101.0, "t1", "f1")
        # first partial fill is NOT complete
        self.assertIs(agg.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(agg.filled_quantity, 25)
        self.assertEqual(agg.remaining_quantity, 75)
        agg.add_fill(50, 99.0, "t2", "f2")
        self.assertEqual(agg.filled_quantity, 75)
        self.assertEqual(agg.remaining_quantity, 25)
        agg.add_fill(25, 100.0, "t3", "f3")
        self.assertIs(agg.state, OrderState.FILLED)
        self.assertEqual(agg.filled_quantity, 100)
        self.assertEqual(agg.remaining_quantity, 0)
        # volume-weighted average price
        expected = (25 * 101.0 + 50 * 99.0 + 25 * 100.0) / 100
        self.assertAlmostEqual(agg.average_fill_price, expected, places=6)
        self.assertAlmostEqual(agg.capital_used, expected * 100, places=6)

    def test_capital_used_uses_actual_fills_only(self):
        agg = FillAggregator(requested_quantity=100)
        agg.add_fill(25, 50.0)
        # requested 100 @ theoretical 50 = 5000, but ACTUAL filled is 25 × 50
        self.assertAlmostEqual(agg.capital_used, 1250.0)

    def test_cancel_with_partial_fill_keeps_fills(self):
        agg = FillAggregator(requested_quantity=100)
        agg.add_fill(30, 55.0)
        agg.apply_broker_status("cancelled")
        self.assertIs(agg.state, OrderState.CANCELLED)
        # position/trade must reflect ACTUAL filled quantity only
        self.assertEqual(agg.filled_quantity, 30)
        self.assertEqual(agg.remaining_quantity, 70)
        self.assertAlmostEqual(agg.capital_used, 30 * 55.0)

    def test_overfill_rejected(self):
        agg = FillAggregator(requested_quantity=100)
        agg.add_fill(75, 100.0)
        with self.assertRaises(ValueError):
            agg.add_fill(50, 100.0)  # would exceed requested — duplicate fill

    def test_fully_filled_order_cannot_take_more_fills(self):
        agg = FillAggregator(requested_quantity=10)
        agg.add_fill(10, 10.0)
        with self.assertRaises(ValueError):
            agg.add_fill(1, 10.0)

    def test_filled_reported_without_fills_goes_reconciling(self):
        agg = FillAggregator(requested_quantity=100)
        agg.apply_broker_status("complete")
        self.assertIs(agg.state, OrderState.RECONCILING)

    def test_rejected_with_fills_goes_reconciling(self):
        """A broker claiming REJECTED while we hold fills is contradictory —
        reconciliation, not blind acceptance."""
        agg = FillAggregator(requested_quantity=100)
        agg.add_fill(25, 100.0)
        agg.apply_broker_status("rejected")
        self.assertIs(agg.state, OrderState.RECONCILING)

    def test_clean_rejected_without_fills(self):
        agg = FillAggregator(requested_quantity=100)
        agg.apply_broker_status("rejected")
        self.assertIs(agg.state, OrderState.REJECTED)
        self.assertEqual(agg.filled_quantity, 0)

    def test_unknown_status_does_not_corrupt_state(self):
        agg = FillAggregator(requested_quantity=100)
        agg.add_fill(25, 100.0)
        before = agg.state
        self.assertIs(agg.apply_broker_status("WEIRD_NEW_STATUS"), before)
        self.assertIs(agg.state, OrderState.PARTIALLY_FILLED)


if __name__ == "__main__":
    unittest.main()
