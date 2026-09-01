"""Unit tests for ConfidenceScorer and OptionPremiumStrategy setup detection."""
import unittest
from backend.strategy.confidence_scoring import (
    ConfidenceScorer,
    SetupScoreResult,
    calculate_ema,
    calculate_rsi,
    calculate_atr,
    calculate_vwap,
    choppiness_index,
    ema_slope,
)
from backend.strategy.strategies.option_premium import OptionPremiumStrategy
from backend.strategy.signal import SignalType


class TestConfidenceScoring(unittest.TestCase):
    def setUp(self):
        self.scorer = ConfidenceScorer()

    def _generate_trending_candles(self, n=60, direction="UP"):
        candles = []
        base = 25000.0
        for i in range(n):
            if direction == "UP":
                c = base + i * 15.0
            else:
                c = base - i * 15.0
            candles.append({
                "timestamp": f"2024-10-01T{9 + (i*5)//60:02d}:{(i*5)%60:02d}:00+05:30",
                "open": c - 5.0,
                "high": c + 10.0,
                "low": c - 8.0,
                "close": c,
                "volume": 20000 + i * 500,
            })
        return candles

    def test_indicators_math(self):
        closes = [100.0 + i for i in range(60)]
        ema20 = calculate_ema(closes, 20)
        self.assertEqual(len(ema20), len(closes))
        self.assertGreater(ema20[-1], ema20[0])

        rsi_vals = calculate_rsi(closes, 14)
        self.assertGreater(len(rsi_vals), 0)
        self.assertGreater(rsi_vals[-1], 50.0)

    def test_bullish_momentum_continuation(self):
        candles = self._generate_trending_candles(60, direction="UP")
        res = self.scorer.evaluate(candles)
        self.assertIsInstance(res, SetupScoreResult)
        self.assertEqual(res.direction, "CE")
        self.assertGreaterEqual(res.confidence, 70.0)
        self.assertIn("MOMENTUM", res.setup_name)

    def test_bearish_momentum_continuation(self):
        candles = self._generate_trending_candles(60, direction="DOWN")
        res = self.scorer.evaluate(candles)
        self.assertIsInstance(res, SetupScoreResult)
        self.assertEqual(res.direction, "PE")
        self.assertGreaterEqual(res.confidence, 70.0)

    def test_option_premium_strategy_evaluation(self):
        strat = OptionPremiumStrategy(min_confidence_to_trade=70.0)
        candles = self._generate_trending_candles(60, direction="UP")
        # 10:00 AM IST entry timestamp
        sig = strat.evaluate("NIFTY50", candles, context={"evaluation_date": "2024-10-01", "current_bar_timestamp": "2024-10-01T10:00:00+05:30"})
        self.assertEqual(sig.signal, SignalType.BUY)
        self.assertEqual(sig.indicators.get("directional_intent"), "CE")
        self.assertGreaterEqual(sig.confidence, 70.0)
        self.assertGreater(sig.stop_loss, 0.0)
        self.assertGreater(sig.target, sig.entry_price)

    def test_session_manager_entry_windows(self):
        from backend.strategy.session_manager import session_manager
        # 09:16 IST (Before 09:20 open filter)
        valid, reason = session_manager.is_valid_entry_time("2024-10-01T09:16:00+05:30")
        self.assertFalse(valid)
        self.assertIn("SESSION_BEFORE_ENTRY_START", reason)

        # 10:15 IST (Morning window: 09:20 - 11:30)
        valid, reason = session_manager.is_valid_entry_time("2024-10-01T10:15:00+05:30")
        self.assertTrue(valid)

        # 12:15 IST (Midday chop window: 11:30 - 13:00)
        valid, reason = session_manager.is_valid_entry_time("2024-10-01T12:15:00+05:30")
        self.assertFalse(valid)
        self.assertIn("SESSION_MIDDAY_LULL", reason)

        # 13:45 IST (Afternoon window: 13:00 - 14:45)
        valid, reason = session_manager.is_valid_entry_time("2024-10-01T13:45:00+05:30")
        self.assertTrue(valid)

        # 15:00 IST (Late day: after 14:45)
        valid, reason = session_manager.is_valid_entry_time("2024-10-01T15:00:00+05:30")
        self.assertFalse(valid)

        # 15:15 IST Mandatory square-off
        self.assertTrue(session_manager.is_mandatory_square_off("2024-10-01T15:15:00+05:30"))
        self.assertFalse(session_manager.is_mandatory_square_off("2024-10-01T14:30:00+05:30"))

    def test_strategy_check_exit_square_off(self):
        strat = OptionPremiumStrategy()
        position = {
            "entry_price": 100.0,
            "stop_loss": 80.0,
            "target": 140.0,
            "trailing_stop": 80.0,
            "entry_time": "2024-10-01T09:30:00+05:30",
        }
        candles = self._generate_trending_candles(20, direction="UP")
        # Exit at 15:15 IST
        exit_sig = strat.check_exit(
            position,
            candles,
            context={"current_bar_timestamp": "2024-10-01T15:15:00+05:30"}
        )
        self.assertEqual(exit_sig, "INTRADAY_SQUARE_OFF")

    def test_strategy_check_exit_expiry_day(self):
        strat = OptionPremiumStrategy()
        position = {
            "entry_price": 100.0,
            "stop_loss": 80.0,
            "target": 140.0,
            "trailing_stop": 80.0,
            "entry_time": "2024-10-01T09:30:00+05:30",
        }
        candles = self._generate_trending_candles(20, direction="UP")
        # Exit on expiry day at 14:35 IST
        exit_sig = strat.check_exit(
            position,
            candles,
            context={"current_bar_timestamp": "2024-10-01T14:35:00+05:30", "is_expiry_day": True}
        )
        self.assertEqual(exit_sig, "EXPIRY_DAY_RISK_EXIT")


if __name__ == "__main__":
    unittest.main()
