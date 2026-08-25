"""Test and verification suite for Upstox Expired Options API, Cache, and Contract Resolution."""
import os
import json
import tempfile
import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch

from backend.broker.upstox_expired_options import (
    UpstoxExpiredOptionsClient,
    OptionsDataCache,
    OptionsDataValidator,
    UpstoxExpiredAPIError,
)
from backend.backtest.options_data_layer import (
    HistoricalOptionsDataLoader,
    HistoricalOptionRecord,
)
from backend.backtest.engine import BacktestEngine
from backend.strategy.signal import StrategySignal, SignalType


class TestUpstoxExpiredOptions(unittest.TestCase):

    def test_validator_rejects_spot_price_substitution(self):
        """Validator must reject candles where option price is suspiciously equal to spot price."""
        validator = OptionsDataValidator()
        
        # Fake candle where close price is 24500 (index spot level) instead of option premium (~200)
        invalid_candles = [
            {"timestamp": "2024-06-25T09:15:00", "open": 24450.0, "high": 24550.0, "low": 24400.0, "close": 24500.0, "volume": 1000},
        ]
        valid, err, _ = validator.validate_candles(
            invalid_candles,
            expected_instrument_key="NSE_FO|NIFTY2462724500CE",
            spot_price_reference=24500.0,
        )
        self.assertFalse(valid)
        self.assertIn("Suspicious option price", err)

    def test_validator_rejects_invalid_ohlc_and_duplicates(self):
        """Validator must reject inverted OHLC (High < Low) and duplicate timestamps."""
        validator = OptionsDataValidator()

        # Inverted High < Low
        bad_ohlc = [
            {"timestamp": "2024-06-25T09:15:00", "open": 180.0, "high": 170.0, "low": 190.0, "close": 175.0, "volume": 100},
        ]
        valid, err, _ = validator.validate_candles(bad_ohlc, "NSE_FO|NIFTY2462724500CE")
        self.assertFalse(valid)
        self.assertIn("High < Low", err)

        # Duplicate timestamps
        dupes = [
            {"timestamp": "2024-06-25T09:15:00", "open": 180.0, "high": 190.0, "low": 175.0, "close": 185.0, "volume": 100},
            {"timestamp": "2024-06-25T09:15:00", "open": 185.0, "high": 195.0, "low": 180.0, "close": 190.0, "volume": 100},
        ]
        valid2, err2, _ = validator.validate_candles(dupes, "NSE_FO|NIFTY2462724500CE")
        self.assertFalse(valid2)
        self.assertIn("Duplicate timestamp", err2)

    def test_validator_contract_metadata(self):
        """Validator must check underlying, strike, expiry, and option type match."""
        validator = OptionsDataValidator()
        contract_info = {
            "underlying": "NIFTY50",
            "expiry": "2024-06-27",
            "strike": 24500.0,
            "option_type": "CE",
        }
        
        # Valid match
        ok, err = validator.validate_contract_metadata(contract_info, "NIFTY50", "2024-06-27", 24500.0, "CE")
        self.assertTrue(ok)
        self.assertIsNone(err)

        # Mismatched strike
        ok_bad_strike, err_strike = validator.validate_contract_metadata(contract_info, "NIFTY50", "2024-06-27", 24600.0, "CE")
        self.assertFalse(ok_bad_strike)
        self.assertIn("Strike mismatch", err_strike)

        # Mismatched option type
        ok_bad_type, err_type = validator.validate_contract_metadata(contract_info, "NIFTY50", "2024-06-27", 24500.0, "PE")
        self.assertFalse(ok_bad_type)
        self.assertIn("Option type mismatch", err_type)

    def test_options_cache_deterministic_naming_and_persistence(self):
        """Cache must store and retrieve historical option candles using deterministic file names."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache = OptionsDataCache(cache_dir=tmp_dir)
            filename = cache.get_cache_filename(
                underlying="NIFTY50",
                expiry="2024-06-27",
                strike=24500.0,
                option_type="CE",
                interval="5minute",
                from_date="2024-06-25",
                to_date="2024-06-27",
            )
            self.assertEqual(filename, "NIFTY50_20240627_24500_CE_5minute_20240625_20240627.json")

            contract_info = {
                "underlying": "NIFTY50",
                "expiry": "2024-06-27",
                "strike": 24500.0,
                "option_type": "CE",
                "instrument_key": "NSE_FO|NIFTY2462724500CE",
            }
            candles = [
                {"timestamp": "2024-06-25T09:15:00", "open": 180.0, "high": 195.0, "low": 175.0, "close": 190.0, "volume": 50000, "oi": 120000},
                {"timestamp": "2024-06-25T09:20:00", "open": 190.0, "high": 220.0, "low": 185.0, "close": 215.0, "volume": 75000, "oi": 125000},
            ]

            saved_path = cache.save(filename, contract_info, candles)
            self.assertTrue(os.path.exists(saved_path))

            loaded = cache.get(filename)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["contract"]["strike"], 24500.0)
            self.assertEqual(len(loaded["candles"]), 2)
            self.assertEqual(loaded["candles"][0]["close"], 190.0)

    def test_single_contract_mock_execution_and_price_validation(self):
        """Test full pipeline with a verified single historical contract:
        Underlying: NIFTY50
        Expiry: 2024-06-27
        Strike: 24500.0
        CE / PE: CE
        Instrument Key: NSE_FO|NIFTY2462724500CE
        Verify: Entry price is option premium (215.0), not index spot (24505.0)
        """
        mock_client = MagicMock(spec=UpstoxExpiredOptionsClient)
        verified_data = {
            "contract": {
                "underlying": "NIFTY50",
                "expiry": "2024-06-27",
                "strike": 24500.0,
                "option_type": "CE",
                "instrument_key": "NSE_FO|NIFTY2462724500CE",
            },
            "candles": [
                {"timestamp": "2024-06-25T09:15:00", "open": 180.0, "high": 195.0, "low": 175.0, "close": 190.0, "volume": 50000, "oi": 120000},
                {"timestamp": "2024-06-25T09:20:00", "open": 190.0, "high": 220.0, "low": 185.0, "close": 215.0, "volume": 75000, "oi": 125000},
                {"timestamp": "2024-06-25T09:25:00", "open": 215.0, "high": 230.0, "low": 210.0, "close": 225.0, "volume": 60000, "oi": 130000},
            ]
        }
        mock_client.resolve_option_contract.return_value = verified_data["contract"]
        mock_client.fetch_and_cache_contract.return_value = (True, None, verified_data)

        loader = HistoricalOptionsDataLoader(upstox_client=mock_client, auto_load_cache=False)

        # Resolution probe
        res = loader.resolve_contract("NIFTY50", date(2024, 6, 25), 24505.0, "CE")
        self.assertIsNotNone(res)
        c_key, exp_str, strike, opt_type = res
        self.assertEqual(c_key, "NSE_FO|NIFTY2462724500CE")
        self.assertEqual(exp_str, "2024-06-27")
        self.assertEqual(strike, 24500.0)
        self.assertEqual(opt_type, "CE")

        # Candle lookup
        candle = loader.get_candle_at(c_key, "2024-06-25T09:20:00")
        self.assertIsNotNone(candle)
        self.assertEqual(candle.close, 215.0)
        self.assertEqual(candle.volume, 75000)
        self.assertEqual(candle.oi, 125000)

        # Backtest engine verification
        engine = BacktestEngine(min_candles_required=2)
        spot_candles = [
            {"timestamp": "2024-06-25T09:10:00", "open": 24490, "high": 24510, "low": 24480, "close": 24500, "volume": 1000000},
            {"timestamp": "2024-06-25T09:15:00", "open": 24500, "high": 24520, "low": 24495, "close": 24502, "volume": 1200000},
            {"timestamp": "2024-06-25T09:20:00", "open": 24502, "high": 24515, "low": 24498, "close": 24505, "volume": 1500000},
            {"timestamp": "2024-06-25T09:25:00", "open": 24505, "high": 24530, "low": 24500, "close": 24520, "volume": 1100000},
        ]

        def fake_evaluate(symbol, window, context=None, strategy_names=None):
            curr_bar = window[-1]
            if curr_bar["timestamp"] == "2024-06-25T09:20:00":
                return [StrategySignal(
                    strategy_name="OPTION_PREMIUM",
                    symbol=symbol,
                    signal=SignalType.BUY,
                    entry_price=curr_bar["close"], # Spot 24505
                    stop_loss=24450.0,
                    target=24600.0,
                    confidence=0.85,
                )]
            return [StrategySignal(strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.NONE)]

        engine.strategy_engine.evaluate = MagicMock(side_effect=fake_evaluate)

        bt_res = engine.run(
            {"NIFTY50": spot_candles},
            options_data_loader=loader,
            require_real_options=True,
        )

        self.assertEqual(bt_res.trades_taken, 1)
        trade = bt_res.trade_log[0]
        # Verify option execution price (215.0) != index spot (24505.0)
        self.assertEqual(trade["entry_price"], 215.0)
        self.assertNotEqual(trade["entry_price"], 24505.0)
        self.assertEqual(trade["symbol"], "NSE_FO|NIFTY2462724500CE")


if __name__ == "__main__":
    unittest.main()
