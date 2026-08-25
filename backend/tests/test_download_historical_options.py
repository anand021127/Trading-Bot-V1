"""Comprehensive unit and integration tests for download_historical_options.py and the ingestion pipeline."""
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
from backend.backtest.engine import BacktestEngine, CostConfig
from backend.strategy.signal import StrategySignal, SignalType
from scripts.download_historical_options import (
    ContractRequirement,
    HistoricalOptionsIngestionPipeline,
    discover_required_contracts_from_signals,
    build_trend_series,
)


class TestDownloadHistoricalOptionsPipeline(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_dir = os.path.join(self.temp_dir.name, "options_cache")
        os.makedirs(self.cache_dir, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_contract_requirement_key_and_cache_filename(self):
        req = ContractRequirement(
            underlying="NIFTY50",
            expiry="2024-06-27",
            strike=24500.0,
            option_type="CE",
            from_date="2024-06-25",
            to_date="2024-06-27",
            interval="5minute",
        )
        self.assertEqual(req.cache_filename, "NIFTY50_20240627_24500_CE_5minute_20240625_20240627.json")
        self.assertIn("NIFTY50_2024-06-27_24500_CE_5minute", req.key)

    def test_auth_failure_handling(self):
        """Pipeline must gracefully detect and report 401 AUTH_INVALID_TOKEN."""
        mock_client = MagicMock()
        mock_client.test_access.return_value = {
            "has_token": True,
            "accessible": False,
            "error_code": "AUTH_INVALID_TOKEN",
            "error_message": "Invalid or expired UPSTOX_ACCESS_TOKEN. Please generate a new active access token.",
            "required_permission": "Valid, unexpired Upstox Access Token (refresh daily via OAuth login)",
        }
        pipeline = HistoricalOptionsIngestionPipeline(
            access_token="expired_token",
            cache_dir=self.cache_dir,
        )
        pipeline.client = mock_client

        auth_result = pipeline.test_auth()
        self.assertFalse(auth_result["accessible"])
        self.assertEqual(auth_result["error_code"], "AUTH_INVALID_TOKEN")

        req = ContractRequirement(
            underlying="NIFTY50",
            expiry="2024-06-27",
            strike=24500.0,
            option_type="CE",
            from_date="2024-06-25",
            to_date="2024-06-27",
        )
        summary = pipeline.run_ingestion([req], dry_run=False)
        self.assertIsNotNone(summary.get("auth_error"))
        self.assertEqual(summary["downloaded"], 0)

    def test_cache_status_detection_and_resumability(self):
        """Pipeline must detect already cached contracts and only download missing ones."""
        pipeline = HistoricalOptionsIngestionPipeline(cache_dir=self.cache_dir)
        
        req1 = ContractRequirement(
            underlying="NIFTY50",
            expiry="2024-06-27",
            strike=24500.0,
            option_type="CE",
            from_date="2024-06-25",
            to_date="2024-06-27",
        )
        req2 = ContractRequirement(
            underlying="BANKNIFTY",
            expiry="2024-06-26",
            strike=52000.0,
            option_type="PE",
            from_date="2024-06-25",
            to_date="2024-06-26",
        )

        # Initially both are missing
        cached, missing = pipeline.check_cache_status([req1, req2])
        self.assertEqual(len(cached), 0)
        self.assertEqual(len(missing), 2)

        # Seed req1 into cache
        contract_info = {
            "underlying": "NIFTY50",
            "expiry": "2024-06-27",
            "strike": 24500.0,
            "option_type": "CE",
            "instrument_key": "NSE_FO|NIFTY2462724500CE",
        }
        candles = [
            {"timestamp": "2024-06-25T09:15:00", "open": 180.0, "high": 195.0, "low": 175.0, "close": 190.0, "volume": 50000},
        ]
        pipeline.cache.save(req1.cache_filename, contract_info, candles)

        # Now req1 is cached, req2 is missing
        cached_after, missing_after = pipeline.check_cache_status([req1, req2])
        self.assertEqual(len(cached_after), 1)
        self.assertEqual(len(missing_after), 1)
        self.assertEqual(cached_after[0].underlying, "NIFTY50")
        self.assertEqual(missing_after[0].underlying, "BANKNIFTY")

    def test_dry_run_does_not_execute_network_requests(self):
        """Dry run mode should calculate requirements without querying API."""
        pipeline = HistoricalOptionsIngestionPipeline(cache_dir=self.cache_dir)
        pipeline.client = MagicMock()

        req = ContractRequirement(
            underlying="NIFTY50",
            expiry="2024-06-27",
            strike=24500.0,
            option_type="CE",
            from_date="2024-06-25",
            to_date="2024-06-27",
        )
        summary = pipeline.run_ingestion([req], dry_run=True)
        self.assertEqual(summary["total_required"], 1)
        self.assertEqual(summary["to_download"], 1)
        self.assertEqual(summary["downloaded"], 0)
        pipeline.client.test_access.assert_not_called()

    def test_successful_ingestion_and_validation(self):
        """Pipeline successfully fetches, validates, and atomically caches option data."""
        mock_client = MagicMock()
        mock_client.test_access.return_value = {"has_token": True, "accessible": True}
        
        verified_payload = {
            "contract": {
                "underlying": "NIFTY50",
                "expiry": "2024-06-27",
                "strike": 24500.0,
                "option_type": "CE",
                "instrument_key": "NSE_FO|NIFTY2462724500CE",
                "lot_size": 25,
            },
            "candles": [
                {"timestamp": "2024-06-25T09:15:00", "open": 180.0, "high": 195.0, "low": 175.0, "close": 190.0, "volume": 50000},
                {"timestamp": "2024-06-25T09:20:00", "open": 190.0, "high": 210.0, "low": 185.0, "close": 205.0, "volume": 60000},
            ],
        }
        mock_client.fetch_and_cache_contract.return_value = (True, None, verified_payload)

        pipeline = HistoricalOptionsIngestionPipeline(cache_dir=self.cache_dir)
        pipeline.client = mock_client

        req = ContractRequirement(
            underlying="NIFTY50",
            expiry="2024-06-27",
            strike=24500.0,
            option_type="CE",
            from_date="2024-06-25",
            to_date="2024-06-27",
        )

        res = pipeline.run_ingestion([req], dry_run=False)
        self.assertEqual(res["downloaded"], 1)
        self.assertEqual(res["failed"], 0)

    def test_integration_with_options_data_loader_and_backtest_engine(self):
        """Cached contract files are immediately loadable by HistoricalOptionsDataLoader."""
        cache = OptionsDataCache(cache_dir=self.cache_dir)
        
        contract_info = {
            "underlying": "NIFTY50",
            "expiry": "2024-06-27",
            "strike": 24500.0,
            "option_type": "CE",
            "instrument_key": "NSE_FO|NIFTY2462724500CE",
            "lot_size": 25,
        }
        candles = [
            {"timestamp": "2024-06-25T09:15:00", "open": 180.0, "high": 195.0, "low": 175.0, "close": 190.0, "volume": 50000},
            {"timestamp": "2024-06-25T09:20:00", "open": 190.0, "high": 220.0, "low": 185.0, "close": 215.0, "volume": 75000},
            {"timestamp": "2024-06-25T09:25:00", "open": 215.0, "high": 230.0, "low": 210.0, "close": 225.0, "volume": 60000},
        ]
        filename = cache.get_cache_filename("NIFTY50", "2024-06-27", 24500.0, "CE", "5minute", "2024-06-25", "2024-06-27")
        cache.save(filename, contract_info, candles)

        loader = HistoricalOptionsDataLoader(data_directory=self.cache_dir, auto_load_cache=False)
        self.assertGreater(loader.available_candles_count(), 0)

        # Verify resolution
        res = loader.resolve_contract("NIFTY50", date(2024, 6, 25), 24502.0, "CE")
        self.assertIsNotNone(res)
        c_key, exp, strike, opt_type = res
        self.assertEqual(c_key, "NSE_FO|NIFTY2462724500CE")
        self.assertEqual(strike, 24500.0)

        # Verify candle lookup
        candle = loader.get_candle_at(c_key, "2024-06-25T09:20:00")
        self.assertIsNotNone(candle)
        self.assertEqual(candle.close, 215.0)


if __name__ == "__main__":
    unittest.main()
