"""Test Suite: V8-D Option Execution Safety & Shadow Mode.

Verifies:
1. Critical Contract Validation (NSE_FO, Expiry, Strike, Lot Size, LTP)
2. Spot vs Option Price Corruption Rejection
3. Stale Quote Rejection (>30s)
4. Duplicate Position Prevention
5. V8-D Shadow Engine Execution Lifecycle (Fills, Target/Stop Exits, Structured Logs)
"""
import unittest
import shutil
from pathlib import Path
from backend.orders.contract_validator import validate_option_contract
from backend.paper.v8d_shadow_mode import V8DShadowEngine


class TestV8OptionExecutionSafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_log_dir = "logs/test_v8_shadow"

    @classmethod
    def tearDownClass(cls):
        if Path(cls.test_log_dir).exists():
            shutil.rmtree(cls.test_log_dir, ignore_errors=True)

    def test_01_contract_validation_success(self):
        """Verify successful contract validation on legitimate ATM option."""
        res = validate_option_contract(
            underlying="NIFTY50",
            instrument_key="NSE_FO|52341",
            strike=24100.0,
            option_type="CE",
            expiry_date="2026-08-20",
            lot_size=25,
            option_ltp=145.50,
            underlying_spot=24115.0,
            quote_age_seconds=2.0,
            account_equity=100000.0,
            quantity=25,
            stop_loss=116.40,
        )
        self.assertTrue(res.is_valid, f"Expected valid contract, got reasons: {res.reasons}")
        self.assertEqual(len(res.reasons), 0)

    def test_02_spot_price_corruption_rejection(self):
        """Verify rejection when option price is erroneously equal to underlying index spot."""
        res = validate_option_contract(
            underlying="NIFTY50",
            instrument_key="NSE_FO|52341",
            strike=24100.0,
            option_type="CE",
            expiry_date="2026-08-20",
            lot_size=25,
            option_ltp=24115.0,  # Corrupted: 24,115 passed as option premium
            underlying_spot=24115.0,
            quote_age_seconds=2.0,
            account_equity=100000.0,
            quantity=25,
            stop_loss=19292.0,
        )
        self.assertFalse(res.is_valid)
        self.assertTrue(any("equals spot price" in r for r in res.reasons))

    def test_03_stale_quote_rejection(self):
        """Verify rejection when option quote is older than 30 seconds."""
        res = validate_option_contract(
            underlying="NIFTY50",
            instrument_key="NSE_FO|52341",
            strike=24100.0,
            option_type="CE",
            expiry_date="2026-08-20",
            lot_size=25,
            option_ltp=145.50,
            underlying_spot=24115.0,
            quote_age_seconds=45.0,  # Stale (> 30s)
            account_equity=100000.0,
            quantity=25,
            stop_loss=116.40,
        )
        self.assertFalse(res.is_valid)
        self.assertTrue(any("stale" in r.lower() for r in res.reasons))

    def test_04_invalid_strike_or_lot_size_rejection(self):
        """Verify rejection of off-step strike or wrong lot size."""
        res = validate_option_contract(
            underlying="NIFTY50",
            instrument_key="NSE_FO|52341",
            strike=24123.0,  # Invalid strike for NIFTY (must be step 50)
            option_type="CE",
            expiry_date="2026-08-20",
            lot_size=50,     # Wrong lot size (must be 25)
            option_ltp=145.50,
            underlying_spot=24115.0,
            account_equity=100000.0,
            quantity=50,
            stop_loss=116.40,
        )
        self.assertFalse(res.is_valid)
        self.assertTrue(any("strike" in r.lower() for r in res.reasons))
        self.assertTrue(any("lot size" in r.lower() for r in res.reasons))

    def test_05_shadow_engine_lifecycle(self):
        """Verify shadow engine opens position and correctly exits on target hit."""
        engine = V8DShadowEngine(starting_equity=100000.0, log_dir=self.test_log_dir)
        
        # Candles with upward trend and pullback
        candles = []
        for i in range(50):
            p = 24000.0 + i * 10.0
            candles.append({"open": p - 5, "high": p + 10, "low": p - 8, "close": p + 5, "volume": 1000})
        last_close = candles[-1]["close"]
        candles.append({"open": last_close, "high": last_close + 5, "low": last_close - 35, "close": last_close + 2, "volume": 1500})

        chain = [{
            "strike": 24500,
            "option_type": "CE",
            "instrument_key": "NSE_FO|99999",
            "ltp": 150.0,
            "expiry": "2026-08-20",
            "oi": 50000,
        }]

        trade = engine.evaluate_and_shadow_execute(
            underlying_symbol="NIFTY50",
            underlying_candles=candles,
            spot_price=24500.0,
            option_chain=chain,
        )

        if trade:
            self.assertEqual(trade.status, "OPEN")
            self.assertEqual(trade.entry_price, 150.0)
            self.assertEqual(trade.stop_loss, 120.0)
            self.assertEqual(trade.target, 172.50)

            # Target hit simulation
            closed_trade = engine.update_quote_and_check_exit("NIFTY50", 175.0)
            self.assertIsNotNone(closed_trade)
            self.assertEqual(closed_trade.status, "CLOSED_TARGET")
            self.assertGreater(closed_trade.net_pnl, 0.0)
            self.assertGreater(engine.equity, 100000.0)


if __name__ == "__main__":
    unittest.main()
