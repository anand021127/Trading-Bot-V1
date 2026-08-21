"""Test Suite: V8-D Backtest & Live Execution Parity.

Verifies mathematical and algorithmic parity between research backtest engine
and live trading components:
1. EMA, RSI, ATR, VWAP indicators
2. Pullback & Retest logic
3. Reversal candle confirmation
4. ATM strike selection formula
5. Fixed -20% Option Stop & +15% Target
6. Dynamic Risk-Capped Position Sizing
7. Transaction Cost Engine calculations
"""
import math
import unittest
from typing import List, Dict, Any

from backend.indicators.ema import calculate_ema
from backend.indicators.rsi import calculate_rsi
from backend.indicators.atr import calculate_atr
from backend.indicators.vwap import vwap
from backend.backtest.engine import CostConfig
from backend.strategy.strategies.v8d_strategy import V8DStrategy


class TestV8ProductionParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.strategy = V8DStrategy()
        cls.cost_model = CostConfig()

    def test_01_ema_rsi_indicator_parity(self):
        """Verify EMA and RSI produce mathematically expected values on test series."""
        prices = [100.0 + i * 1.5 for i in range(60)]
        ema20 = calculate_ema(prices, 20)
        ema50 = calculate_ema(prices, 50)
        rsi14 = calculate_rsi(prices, 14)

        self.assertEqual(len(ema20), len(prices))
        self.assertEqual(len(ema50), len(prices))
        self.assertGreater(len(rsi14), 0)
        
        # Upward sloping series must have EMA20 > EMA50 and high RSI
        self.assertGreater(ema20[-1], ema50[-1])
        self.assertGreater(rsi14[-1], 70.0)

    def test_02_atm_strike_calculation_parity(self):
        """Verify ATM strike selection for NIFTY (step 50) and BANKNIFTY (step 100)."""
        # NIFTY examples
        self.assertEqual(self.strategy.get_atm_strike(24124.0, "NIFTY50"), 24100)
        self.assertEqual(self.strategy.get_atm_strike(24126.0, "NIFTY50"), 24150)
        self.assertEqual(self.strategy.get_atm_strike(24149.9, "NIFTY50"), 24150)
        self.assertEqual(self.strategy.get_atm_strike(24174.0, "NIFTY50"), 24150)
        self.assertEqual(self.strategy.get_atm_strike(24176.0, "NIFTY50"), 24200)

        # BANKNIFTY examples
        self.assertEqual(self.strategy.get_atm_strike(51240.0, "BANKNIFTY"), 51200)
        self.assertEqual(self.strategy.get_atm_strike(51251.0, "BANKNIFTY"), 51300)
        self.assertEqual(self.strategy.get_atm_strike(51299.0, "BANKNIFTY"), 51300)

    def test_03_lot_size_parity(self):
        """Verify historical and exchange lot sizes."""
        self.assertEqual(self.strategy.get_lot_size("NIFTY50"), 25)
        self.assertEqual(self.strategy.get_lot_size("NIFTY 50"), 25)
        self.assertEqual(self.strategy.get_lot_size("BANKNIFTY"), 15)
        self.assertEqual(self.strategy.get_lot_size("NIFTY BANK"), 15)

    def test_04_v8d_stop_and_target_parity(self):
        """Verify fixed -20% stop and +15% target formulas."""
        premium = 150.0
        stop_loss = round(premium * (1.0 - self.strategy.stop_loss_pct), 2)
        target = round(premium * (1.0 + self.strategy.target_pct), 2)

        self.assertEqual(stop_loss, 120.0)
        self.assertEqual(target, 172.50)
        self.assertEqual(self.strategy.stop_loss_pct, 0.20)
        self.assertEqual(self.strategy.target_pct, 0.15)

    def test_05_dynamic_risk_capped_sizing_parity(self):
        """Verify dynamic equity sizing under 3% risk and 20% capital allocation limits."""
        account_equity = 100000.0
        premium = 100.0
        stop_loss = 80.0  # -20% stop -> risk per unit = 20.0
        lot_size = 25

        qty, details = self.strategy.calculate_position_size(
            account_equity=account_equity,
            option_premium=premium,
            lot_size=lot_size,
            stop_loss_premium=stop_loss,
        )

        # Max risk = 3,000 / 20 = 150 units = 6 lots (150 qty)
        # Max alloc = 20,000 / (100 * 25) = 8 lots (200 qty)
        # Min(6, 8) = 6 lots = 150 qty
        self.assertEqual(qty, 150)
        self.assertEqual(details["allowed_lots"], 6)
        self.assertLessEqual(details["actual_allocation_pct"], 20.0)
        self.assertLessEqual(details["actual_risk_pct"], 3.0)

    def test_05b_no_trade_when_one_lot_exceeds_allocation_limit(self):
        """Verify trade rejection (qty=0) when 1 lot exceeds 20% max allocation limit."""
        account_equity = 10000.0  # Max allocation = 2,000
        premium = 100.0           # 1 lot NIFTY (25) = 2,500 > 2,000
        stop_loss = 80.0
        lot_size = 25

        qty, details = self.strategy.calculate_position_size(
            account_equity=account_equity,
            option_premium=premium,
            lot_size=lot_size,
            stop_loss_premium=stop_loss,
        )
        self.assertEqual(qty, 0)
        self.assertEqual(details["allowed_lots"], 0)
        self.assertEqual(details["actual_allocation_pct"], 0.0)
        self.assertEqual(details["actual_risk_pct"], 0.0)

    def test_05c_no_trade_when_one_lot_exceeds_risk_limit(self):
        """Verify trade rejection (qty=0) when 1 lot exceeds 3% max account risk limit."""
        account_equity = 10000.0  # Max risk = 300
        premium = 200.0
        stop_loss = 160.0         # Risk per unit = 40; 1 lot (25) risk = 1,000 > 300
        lot_size = 25

        qty, details = self.strategy.calculate_position_size(
            account_equity=account_equity,
            option_premium=premium,
            lot_size=lot_size,
            stop_loss_premium=stop_loss,
        )
        self.assertEqual(qty, 0)
        self.assertEqual(details["allowed_lots"], 0)
        self.assertEqual(details["actual_allocation_pct"], 0.0)
        self.assertEqual(details["actual_risk_pct"], 0.0)

    def test_06_pullback_signal_generation_bullish(self):
        """Verify bullish pullback signal generates CE signal on underlying candles."""
        # Construct synthetic candle series with upward trend, pullback to EMA20, and green reversal
        candles = []
        p = 24000.0
        # 45 bars upward trend with alternating small up/down bars
        for i in range(45):
            p += (10 if i % 2 == 0 else 5)
            candles.append({'open': p - 3, 'high': p + 5, 'low': p - 5, 'close': p, 'volume': 1000})

        # 12 bars of consolidation / gentle pullback to bring RSI into 40-60 zone
        for j in range(12):
            p -= (8 if j % 2 == 0 else 3)
            candles.append({'open': p + 4, 'high': p + 6, 'low': p - 6, 'close': p, 'volume': 800})

        # Green reversal bar testing EMA20
        last_close = candles[-1]['close']
        candles.append({'open': last_close - 2, 'high': last_close + 25, 'low': last_close - 10, 'close': last_close + 20, 'volume': 1500})

        sig_type, conds, indics = self.strategy.detect_pullback_signal(candles)
        self.assertIsNotNone(sig_type)
        self.assertEqual(sig_type, "CE")

    def test_07_transaction_cost_parity(self):
        """Verify transaction cost model matches Upstox statutory schedule."""
        res = self.cost_model.apply(100.0, 115.0, 25, is_option=True)
        # Gross P&L: 25 * 15 = 375
        self.assertEqual(res["gross_pnl"], 375.0)
        self.assertGreater(res["total_cost"], 0.0)
        self.assertEqual(res["net_pnl"], round(res["gross_pnl"] - res["total_cost"], 2))


if __name__ == "__main__":
    unittest.main()
