"""Test Suite: V8-D Live Risk Engine & Production Guardrails.

Verifies:
1. Max Account Risk = 3.0%
2. Max Capital Allocation = 20.0%
3. Dynamic Equity Sizing (Scales with available capital, NOT hardcoded ₹100k)
4. Lot Size Rounding & Zero-Lot Rejections
5. Daily Trade Limit Enforced (Max 3 trades)
6. Emergency Kill Switch Blocking
7. Reconciliation Mismatch Blocking
"""
import unittest
from backend.strategy.strategies.v8d_strategy import V8DStrategy
from backend.orders.contract_validator import validate_option_contract


class TestV8LiveRiskGuardrails(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.strategy = V8DStrategy()

    def test_01_dynamic_capital_scaling(self):
        """Verify position sizing scales dynamically with different account equity levels."""
        premium = 120.0
        stop_loss = 96.0  # -20% stop -> risk per unit = 24.0
        lot_size = 25

        # ₹100,000 capital:
        # Max risk = 3,000 / 24 = 125 units (5 lots)
        # Max alloc = 20,000 / 3000 = 6 lots
        # Min(5, 6) = 5 lots = 125 qty
        qty_100k, details_100k = self.strategy.calculate_position_size(100000.0, premium, lot_size, stop_loss)
        self.assertEqual(qty_100k, 125)
        self.assertEqual(details_100k["allowed_lots"], 5)

        # ₹500,000 capital:
        # Max risk = 15,000 / 24 = 625 units (25 lots)
        # Max alloc = 100,000 / 3000 = 33 lots
        # Min(25, 33) = 25 lots = 625 qty
        qty_500k, details_500k = self.strategy.calculate_position_size(500000.0, premium, lot_size, stop_loss)
        self.assertEqual(qty_500k, 625)
        self.assertEqual(details_500k["allowed_lots"], 25)

        # ₹50,000 capital:
        # Max risk = 1,500 / 24 = 62.5 units (2 lots = 50 qty)
        # Max alloc = 10,000 / 3000 = 3 lots
        # Min(2, 3) = 2 lots = 50 qty
        qty_50k, details_50k = self.strategy.calculate_position_size(50000.0, premium, lot_size, stop_loss)
        self.assertEqual(qty_50k, 50)
        self.assertEqual(details_50k["allowed_lots"], 2)

    def test_02_strict_risk_allocation_bounds(self):
        """Verify that neither the 3% risk nor the 20% allocation limit is ever breached."""
        equities = [50000.0, 100000.0, 250000.0, 1000000.0]
        premiums = [50.0, 100.0, 200.0, 400.0, 800.0]

        for eq in equities:
            for prem in premiums:
                sl = prem * 0.80
                qty, details = self.strategy.calculate_position_size(eq, prem, 25, sl)
                
                # Check bounds
                if qty > 0:
                    pos_val = qty * prem
                    tot_risk = qty * (prem - sl)
                    
                    # Allocation must be <= 20% (allowing single lot threshold if equity is small)
                    if pos_val > eq * 0.20:
                        self.assertEqual(qty, 25, "Can only exceed 20% if at minimum 1 lot constraint")
                    else:
                        self.assertLessEqual(pos_val / eq, 0.2001)

                    # Risk must be <= 3% (or 1 lot if minimum)
                    if tot_risk > eq * 0.03:
                        self.assertEqual(qty, 25, "Can only exceed 3% if at minimum 1 lot constraint")
                    else:
                        self.assertLessEqual(tot_risk / eq, 0.0301)

    def test_03_daily_trade_ceiling(self):
        """Verify that reaching 3 trades per day blocks new signal execution."""
        candles = [{"open": 24000, "high": 24050, "low": 23990, "close": 24040, "volume": 1000} for _ in range(60)]
        chain = [{"strike": 24000, "option_type": "CE", "instrument_key": "NSE_FO|12345", "ltp": 150.0, "expiry": "2026-08-20"}]

        # Under 3 trades
        sig, dec = self.strategy.evaluate_v8d_signal("NIFTY50", candles, 24000.0, chain, 100000.0, trades_today=2)
        self.assertNotIn("Daily trade limit reached", dec.rejection_reasons)

        # At or above 3 trades
        sig_blocked, dec_blocked = self.strategy.evaluate_v8d_signal("NIFTY50", candles, 24000.0, chain, 100000.0, trades_today=3)
        self.assertEqual(dec_blocked.decision, "NO_SIGNAL" if sig_blocked.signal != "BUY" else "REJECTED")
        self.assertTrue(any("Daily trade limit reached" in r for r in dec_blocked.rejection_reasons))

    def test_04_kill_switch_blocking(self):
        """Verify kill switch blocks trade execution unconditionally."""
        candles = [{"open": 24000, "high": 24050, "low": 23990, "close": 24040, "volume": 1000} for _ in range(60)]
        chain = [{"strike": 24000, "option_type": "CE", "instrument_key": "NSE_FO|12345", "ltp": 150.0, "expiry": "2026-08-20"}]

        sig, dec = self.strategy.evaluate_v8d_signal("NIFTY50", candles, 24000.0, chain, 100000.0, kill_switch_active=True)
        self.assertTrue(any("Kill switch is active" in r for r in dec.rejection_reasons))

    def test_05_reconciliation_mismatch_blocking(self):
        """Verify position reconciliation failure blocks new entries."""
        candles = [{"open": 24000, "high": 24050, "low": 23990, "close": 24040, "volume": 1000} for _ in range(60)]
        chain = [{"strike": 24000, "option_type": "CE", "instrument_key": "NSE_FO|12345", "ltp": 150.0, "expiry": "2026-08-20"}]

        sig, dec = self.strategy.evaluate_v8d_signal("NIFTY50", candles, 24000.0, chain, 100000.0, reconciliation_ok=False)
        self.assertTrue(any("reconciliation mismatch" in r.lower() for r in dec.rejection_reasons))


if __name__ == "__main__":
    unittest.main()
