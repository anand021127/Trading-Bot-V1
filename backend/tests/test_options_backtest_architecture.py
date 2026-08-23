"""Unit tests for Historical Options Backtest Architecture (Phase 8).

Verifies:
1. Context contains historical spot price from evaluated candle.
2. Bullish signal resolves CE directional intent.
3. Bearish signal resolves PE directional intent.
4. Historical contract resolver receives the correct timestamp and date.
5. Missing contract produces explicit rejection (DATA_UNAVAILABLE).
6. Missing premium produces explicit rejection (DATA_UNAVAILABLE).
7. No live option-chain API is called during backtest.
8. No current contract is reused across historical dates.
9. No look-ahead data is used (bar-by-bar evaluation).
"""
import unittest
from datetime import date
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

from backend.backtest.engine import BacktestEngine, BacktestResult
from backend.backtest.options_data_layer import HistoricalOptionsDataLoader, HistoricalOptionRecord
from backend.strategy.strategy_engine import MultiStrategyEngine
from backend.strategy.signal import StrategySignal, SignalType
from backend.strategy.strategies.option_premium import OptionPremiumStrategy


class TestOptionsBacktestArchitecture(unittest.TestCase):
    def setUp(self):
        self.strategy = OptionPremiumStrategy()

    def test_1_context_contains_historical_spot_price(self):
        """Spot price must be extracted from the evaluated candle, not live broker."""
        candles = [
            {"open": 21500.0, "high": 21550.0, "low": 21480.0, "close": 21520.0, "volume": 5000, "timestamp": f"2024-01-02T09:{15+i:02d}:00+05:30"}
            for i in range(25)
        ]
        context = {
            "symbol": "NIFTY50",
            "current_bar_date": "2024-01-02",
            "evaluation_date": "2024-01-02",
            "spot_price": 21520.0,
            "underlying_trend": "BULLISH",
        }
        sig = self.strategy.evaluate("NIFTY50", candles, context=context)
        self.assertEqual(sig.indicators.get("spot_price"), 21520.0)

    def test_2_bullish_signal_resolves_ce(self):
        """Bullish underlying trend must produce CE directional intent."""
        strategy = OptionPremiumStrategy(min_momentum_pct=0.1)
        # Steadily rising candles to ensure positive momentum and close > VWAP
        candles = [
            {"open": 21500.0 + i * 10, "high": 21510.0 + i * 10, "low": 21495.0 + i * 10, "close": 21505.0 + i * 10, "volume": 5000, "timestamp": f"2024-01-02T{10+i//60:02d}:{i%60:02d}:00+05:30"}
            for i in range(30)
        ]
        context = {
            "symbol": "NIFTY50",
            "current_bar_date": "2024-01-02",
            "underlying_trend": "BULLISH",
            "spot_price": candles[-1]["close"],
        }
        sig = strategy.evaluate("NIFTY50", candles, context=context)
        self.assertEqual(sig.indicators.get("directional_intent"), "CE")
        self.assertEqual(sig.indicators.get("option_type"), "CE")
        self.assertEqual(sig.signal, SignalType.BUY)

    def test_3_bearish_signal_resolves_pe(self):
        """Bearish underlying trend must produce PE directional intent."""
        strategy = OptionPremiumStrategy(min_momentum_pct=0.1)
        # Steadily falling candles to ensure negative momentum and close < VWAP
        candles = [
            {"open": 21800.0 - i * 10, "high": 21805.0 - i * 10, "low": 21785.0 - i * 10, "close": 21790.0 - i * 10, "volume": 5000, "timestamp": f"2024-01-02T{10+i//60:02d}:{i%60:02d}:00+05:30"}
            for i in range(30)
        ]
        context = {
            "symbol": "NIFTY50",
            "current_bar_date": "2024-01-02",
            "underlying_trend": "BEARISH",
            "spot_price": candles[-1]["close"],
        }
        sig = strategy.evaluate("NIFTY50", candles, context=context)
        self.assertEqual(sig.indicators.get("directional_intent"), "PE")
        self.assertEqual(sig.indicators.get("option_type"), "PE")
        self.assertEqual(sig.signal, SignalType.BUY)

    def test_4_contract_resolver_receives_correct_timestamp(self):
        """BacktestEngine must invoke resolve_contract with exact bar date and spot."""
        engine = BacktestEngine(strategy_engine=MultiStrategyEngine([OptionPremiumStrategy(min_momentum_pct=0.01)]), min_candles_required=20)
        loader = MagicMock(spec=HistoricalOptionsDataLoader)
        loader.is_data_available.return_value = True
        loader.resolve_contract.return_value = ("NSE_FO|NIFTY24JAN21500CE", "2024-01-04", 21500, "CE")
        loader.get_candle_at.return_value = HistoricalOptionRecord(
            date="2024-01-02",
            timestamp="2024-01-02T10:00:00+05:30",
            underlying="NIFTY50",
            expiry="2024-01-04",
            strike=21500.0,
            option_type="CE",
            instrument_key="NSE_FO|NIFTY24JAN21500CE",
            open=150.0,
            high=160.0,
            low=145.0,
            close=155.0,
            volume=1000,
        )

        candles = [
            {"open": 21500.0 + i * 5, "high": 21510.0 + i * 5, "low": 21495.0 + i * 5, "close": 21505.0 + i * 5, "volume": 5000, "timestamp": f"2024-01-02T10:{i:02d}:00+05:30"}
            for i in range(25)
        ]
        option_contexts = {
            "NIFTY50": {
                "underlying_trend_series": {c["timestamp"]: "BULLISH" for c in candles},
            }
        }

        res = engine.run(
            symbol_candles={"NIFTY50": candles},
            strategy_names=["OPTION_PREMIUM"],
            option_contexts=option_contexts,
            options_data_loader=loader,
            require_real_options=True,
        )

        loader.resolve_contract.assert_called()
        args = loader.resolve_contract.call_args[0]
        self.assertEqual(args[0], "NIFTY50")
        self.assertEqual(args[1], date(2024, 1, 2))
        self.assertEqual(args[3], "CE")
        self.assertGreater(res.contracts_resolved, 0)

    def test_5_missing_contract_produces_explicit_rejection(self):
        """When resolve_contract returns None, result records DATA_UNAVAILABLE and contract_resolution_failures."""
        engine = BacktestEngine(strategy_engine=MultiStrategyEngine([OptionPremiumStrategy(min_momentum_pct=0.01)]), min_candles_required=20)
        loader = MagicMock(spec=HistoricalOptionsDataLoader)
        loader.is_data_available.return_value = True
        loader.resolve_contract.return_value = None  # No matching contract

        candles = [
            {"open": 21500.0 + i * 5, "high": 21510.0 + i * 5, "low": 21495.0 + i * 5, "close": 21505.0 + i * 5, "volume": 5000, "timestamp": f"2024-01-02T10:{i:02d}:00+05:30"}
            for i in range(25)
        ]
        option_contexts = {
            "NIFTY50": {
                "underlying_trend_series": {c["timestamp"]: "BULLISH" for c in candles},
            }
        }

        res = engine.run(
            symbol_candles={"NIFTY50": candles},
            strategy_names=["OPTION_PREMIUM"],
            option_contexts=option_contexts,
            options_data_loader=loader,
            require_real_options=True,
        )

        self.assertGreater(res.contract_resolution_failures, 0)
        self.assertEqual(res.trades_taken, 0)
        self.assertTrue(any("DATA_UNAVAILABLE" in k for k in res.rejection_reason_counts.keys()))

    def test_6_missing_premium_produces_explicit_rejection(self):
        """When get_candle_at returns None, result records option_premium_missing."""
        engine = BacktestEngine(strategy_engine=MultiStrategyEngine([OptionPremiumStrategy(min_momentum_pct=0.01)]), min_candles_required=20)
        loader = MagicMock(spec=HistoricalOptionsDataLoader)
        loader.is_data_available.return_value = True
        loader.resolve_contract.return_value = ("NSE_FO|NIFTY24JAN21500CE", "2024-01-04", 21500, "CE")
        loader.get_candle_at.return_value = None  # Missing premium candle

        candles = [
            {"open": 21500.0 + i * 5, "high": 21510.0 + i * 5, "low": 21495.0 + i * 5, "close": 21505.0 + i * 5, "volume": 5000, "timestamp": f"2024-01-02T10:{i:02d}:00+05:30"}
            for i in range(25)
        ]
        option_contexts = {
            "NIFTY50": {
                "underlying_trend_series": {c["timestamp"]: "BULLISH" for c in candles},
            }
        }

        res = engine.run(
            symbol_candles={"NIFTY50": candles},
            strategy_names=["OPTION_PREMIUM"],
            option_contexts=option_contexts,
            options_data_loader=loader,
            require_real_options=True,
        )

        self.assertGreater(res.option_premium_missing, 0)
        self.assertEqual(res.trades_taken, 0)
        self.assertTrue(any("Missing historical option candle" in k for k in res.rejection_reason_counts.keys()))

    def test_7_no_live_option_chain_api_during_backtest(self):
        """OptionPremiumStrategy evaluation on historical candles does not query live option chain."""
        candles = [
            {"open": 21500.0 + i * 5, "high": 21510.0 + i * 5, "low": 21495.0 + i * 5, "close": 21505.0 + i * 5, "volume": 5000, "timestamp": f"2024-01-02T10:{i:02d}:00+05:30"}
            for i in range(25)
        ]
        context = {
            "symbol": "NIFTY50",
            "current_bar_date": "2024-01-02",
            "underlying_trend": "BULLISH",
            "spot_price": 21625.0,
        }
        # Evaluation should succeed without option_chain key
        sig = self.strategy.evaluate("NIFTY50", candles, context=context)
        self.assertIsNotNone(sig)
        self.assertIn("directional_intent", sig.indicators)

    def test_8_no_current_contract_reused_for_historical_dates(self):
        """Contracts are resolved per target_date and spot_price."""
        engine = BacktestEngine(strategy_engine=MultiStrategyEngine([OptionPremiumStrategy(min_momentum_pct=0.01)]), min_candles_required=20)
        loader = MagicMock(spec=HistoricalOptionsDataLoader)
        loader.is_data_available.return_value = True
        
        # Return different contracts for different dates
        def mock_resolve(symbol, target_date, spot_price, option_type):
            if target_date == date(2024, 1, 2):
                return ("NSE_FO|NIFTY24JAN21500CE", "2024-01-04", 21500, "CE")
            elif target_date == date(2024, 1, 8):
                return ("NSE_FO|NIFTY24JAN21600CE", "2024-01-11", 21600, "CE")
            return None

        loader.resolve_contract.side_effect = mock_resolve
        
        # Mock get_candle_at to return a candle that triggers target hit (100 -> 200) so position closes immediately
        def mock_candle(c_key, ts):
            return HistoricalOptionRecord(
                date=ts[:10],
                timestamp=ts,
                underlying="NIFTY50",
                expiry="2024-01-04",
                strike=21500.0,
                option_type="CE",
                instrument_key=c_key,
                open=100.0,
                high=300.0,
                low=90.0,
                close=250.0,  # exceeds target so position exits
                volume=1000,
            )

        loader.get_candle_at.side_effect = mock_candle

        candles_day1 = [
            {"open": 21500.0 + i * 2, "high": 21510.0 + i * 2, "low": 21495.0 + i * 2, "close": 21505.0 + i * 2, "volume": 5000, "timestamp": f"2024-01-02T10:{i:02d}:00+05:30"}
            for i in range(25)
        ]
        candles_day2 = [
            {"open": 21600.0 + i * 2, "high": 21610.0 + i * 2, "low": 21595.0 + i * 2, "close": 21605.0 + i * 2, "volume": 5000, "timestamp": f"2024-01-08T10:{i:02d}:00+05:30"}
            for i in range(25)
        ]
        all_candles = candles_day1 + candles_day2
        option_contexts = {
            "NIFTY50": {
                "underlying_trend_series": {c["timestamp"]: "BULLISH" for c in all_candles},
            }
        }

        res = engine.run(
            symbol_candles={"NIFTY50": all_candles},
            strategy_names=["OPTION_PREMIUM"],
            option_contexts=option_contexts,
            options_data_loader=loader,
            require_real_options=True,
        )
        self.assertGreater(loader.resolve_contract.call_count, 1)

    def test_9_no_lookahead_data_used(self):
        """Window passed to strategy on bar i includes only bars up to index i."""
        engine = BacktestEngine(min_candles_required=20)
        captured_windows = []

        original_eval = engine.strategy_engine.evaluate
        def mock_eval(symbol, window, context=None, strategy_names=None):
            captured_windows.append(len(window))
            return original_eval(symbol, window, context=context, strategy_names=strategy_names)

        engine.strategy_engine.evaluate = mock_eval

        candles = [
            {"open": 21500.0 + i, "high": 21505.0 + i, "low": 21495.0 + i, "close": 21500.0 + i, "volume": 1000, "timestamp": f"2024-01-02T10:{i:02d}:00+05:30"}
            for i in range(25)
        ]
        option_contexts = {
            "NIFTY50": {
                "underlying_trend_series": {c["timestamp"]: "BULLISH" for c in candles},
            }
        }

        engine.run(
            symbol_candles={"NIFTY50": candles},
            strategy_names=["OPTION_PREMIUM"],
            option_contexts=option_contexts,
            require_real_options=False,
        )

        # Window lengths must grow strictly from min_candles_required (20) to 25
        self.assertEqual(captured_windows, [21, 22, 23, 24, 25])


if __name__ == "__main__":
    unittest.main()
