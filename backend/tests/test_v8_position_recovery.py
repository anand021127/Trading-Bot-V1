"""Test Suite: V8-D Position Recovery & Broker Reconciliation.

Verifies:
1. Exact 1-to-1 match allows trading to resume
2. Orphaned local position halts trading
3. Orphaned broker position halts trading
4. Quantity discrepancy halts trading
5. Application restart resilience
"""
import unittest
from backend.orders.startup_recovery import recover_and_reconcile_positions


class TestV8PositionRecovery(unittest.TestCase):
    def test_01_exact_position_match(self):
        """Verify matched positions between SQLite DB and broker enable trading."""
        sqlite_positions = [{
            "contract_instrument_key": "NSE_FO|52341",
            "symbol": "NIFTY50",
            "quantity": 25,
            "entry_price": 145.0,
        }]
        broker_positions = [{
            "instrument_key": "NSE_FO|52341",
            "quantity": 25,
            "average_price": 145.0,
        }]

        status = recover_and_reconcile_positions(sqlite_positions, broker_positions)
        self.assertTrue(status.reconciled)
        self.assertFalse(status.trading_halted)
        self.assertEqual(len(status.mismatches), 0)

    def test_02_orphaned_local_position_halts_trading(self):
        """Verify position present in DB but missing at broker halts trading."""
        sqlite_positions = [{
            "contract_instrument_key": "NSE_FO|52341",
            "symbol": "NIFTY50",
            "quantity": 25,
        }]
        broker_positions = []  # Broker has no positions

        status = recover_and_reconcile_positions(sqlite_positions, broker_positions)
        self.assertFalse(status.reconciled)
        self.assertTrue(status.trading_halted)
        self.assertEqual(len(status.mismatches), 1)
        self.assertEqual(status.mismatches[0]["type"], "ORPHANED_LOCAL_POSITION")

    def test_03_orphaned_broker_position_halts_trading(self):
        """Verify position present at broker but missing in DB halts trading."""
        sqlite_positions = []
        broker_positions = [{
            "instrument_key": "NSE_FO|99999",
            "quantity": 50,
        }]

        status = recover_and_reconcile_positions(sqlite_positions, broker_positions)
        self.assertFalse(status.reconciled)
        self.assertTrue(status.trading_halted)
        self.assertEqual(len(status.mismatches), 1)
        self.assertEqual(status.mismatches[0]["type"], "ORPHANED_BROKER_POSITION")

    def test_04_quantity_discrepancy_halts_trading(self):
        """Verify mismatched quantities halt trading."""
        sqlite_positions = [{
            "contract_instrument_key": "NSE_FO|52341",
            "quantity": 50,
        }]
        broker_positions = [{
            "instrument_key": "NSE_FO|52341",
            "quantity": 25,  # Partial fill or broker difference
        }]

        status = recover_and_reconcile_positions(sqlite_positions, broker_positions)
        self.assertFalse(status.reconciled)
        self.assertTrue(status.trading_halted)
        self.assertEqual(len(status.mismatches), 1)
        self.assertEqual(status.mismatches[0]["type"], "QUANTITY_MISMATCH")

    def test_05_clean_empty_state_resumes_cleanly(self):
        """Verify zero positions in DB and zero positions at broker is valid state."""
        status = recover_and_reconcile_positions([], [])
        self.assertTrue(status.reconciled)
        self.assertFalse(status.trading_halted)
        self.assertEqual(len(status.mismatches), 0)


if __name__ == "__main__":
    unittest.main()
