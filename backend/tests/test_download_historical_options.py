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
        mock_client.get_expiry_coverage.return_value = {
            "underlying": "NIFTY50",
            "expiries": ["2024-06-27"],
            "earliest_expiry": "2024-06-27",
            "latest_expiry": "2024-06-27",
            "total_expiries": 1,
        }
        mock_client.get_option_contracts.return_value = [
            {
                "underlying": "NIFTY50",
                "expiry": "2024-06-27",
                "strike": 24500.0,
                "option_type": "CE",
                "instrument_key": "NSE_FO|NIFTY2462724500CE",
                "trading_symbol": "NIFTY2462724500CE",
                "lot_size": 25,
            }
        ]
        
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
        self.assertEqual(res["strategy_required_contracts"], 1)
        self.assertEqual(res["already_cached"], 0)
        self.assertEqual(res["outside_upstox_coverage"], 0)
        self.assertEqual(res["not_present_in_catalogue"], 0)
        self.assertEqual(res["eligible_for_download"], 1)
        self.assertEqual(res["successfully_downloaded"], 1)
        self.assertEqual(res["failed_api_downloads"], 0)
        self.assertEqual(res["remaining_unavailable"], 0)
        self.assertEqual(res["downloaded"], 1)
        self.assertEqual(res["failed"], 0)

    def test_expiry_outside_upstox_coverage_marked_data_unavailable(self):
        """Expiry outside Upstox coverage is marked DATA_UNAVAILABLE without querying catalogue or candles."""
        mock_client = MagicMock()
        mock_client.test_access.return_value = {"has_token": True, "accessible": True}
        mock_client.get_expiry_coverage.return_value = {
            "underlying": "NIFTY50",
            "expiries": ["2024-10-03", "2024-10-10"],
            "earliest_expiry": "2024-10-03",
            "latest_expiry": "2024-10-10",
            "total_expiries": 2,
        }

        pipeline = HistoricalOptionsIngestionPipeline(cache_dir=self.cache_dir)
        pipeline.client = mock_client

        req = ContractRequirement(
            underlying="NIFTY50",
            expiry="2024-01-18",
            strike=21700.0,
            option_type="PE",
            from_date="2024-01-15",
            to_date="2024-01-18",
        )

        res = pipeline.run_ingestion([req], dry_run=False)

        # Assert contract catalogue was NOT queried for this expiry
        mock_client.get_option_contracts.assert_not_called()
        mock_client.fetch_and_cache_contract.assert_not_called()

        # Assert status and unavailability reason
        self.assertEqual(req.status, "DATA_UNAVAILABLE")
        self.assertIn("EXPIRY_OUTSIDE_UPSTOX_COVERAGE", req.unavailability_reason)
        self.assertIn("2024-10-03", req.unavailability_reason)

        # Assert summary tallies
        self.assertEqual(res["strategy_required_contracts"], 1)
        self.assertEqual(res["outside_upstox_coverage"], 1)
        self.assertEqual(res["eligible_for_download"], 0)
        self.assertEqual(res["successfully_downloaded"], 0)
        self.assertEqual(res["remaining_unavailable"], 1)
        self.assertEqual(res["error_categories"]["EXPIRY_OUTSIDE_UPSTOX_COVERAGE"], 1)

    def test_contract_absent_from_catalogue_marked_data_unavailable(self):
        """Expiry inside coverage, but strike/type missing in catalogue is marked DATA_UNAVAILABLE."""
        mock_client = MagicMock()
        mock_client.test_access.return_value = {"has_token": True, "accessible": True}
        mock_client.get_expiry_coverage.return_value = {
            "underlying": "NIFTY50",
            "expiries": ["2024-10-03"],
            "earliest_expiry": "2024-10-03",
            "latest_expiry": "2024-10-03",
            "total_expiries": 1,
        }
        # Catalogue only contains 25000 CE, but requirement is 25500 PE
        mock_client.get_option_contracts.return_value = [
            {
                "underlying": "NIFTY50",
                "expiry": "2024-10-03",
                "strike": 25000.0,
                "option_type": "CE",
                "instrument_key": "NSE_FO|NIFTY24O0325000CE",
                "trading_symbol": "NIFTY24O0325000CE",
                "lot_size": 25,
            }
        ]

        pipeline = HistoricalOptionsIngestionPipeline(cache_dir=self.cache_dir)
        pipeline.client = mock_client

        req = ContractRequirement(
            underlying="NIFTY50",
            expiry="2024-10-03",
            strike=25500.0,
            option_type="PE",
            from_date="2024-10-01",
            to_date="2024-10-03",
        )

        res = pipeline.run_ingestion([req], dry_run=False)

        # Assert catalogue was queried once
        mock_client.get_option_contracts.assert_called_once_with("NIFTY50", "2024-10-03")
        # Assert candle download was NOT attempted
        mock_client.fetch_and_cache_contract.assert_not_called()

        self.assertEqual(req.status, "DATA_UNAVAILABLE")
        self.assertIn("CONTRACT_NOT_IN_CATALOGUE", req.unavailability_reason)
        self.assertEqual(res["outside_upstox_coverage"], 0)
        self.assertEqual(res["not_present_in_catalogue"], 1)
        self.assertEqual(res["eligible_for_download"], 0)
        self.assertEqual(res["remaining_unavailable"], 1)
        self.assertEqual(res["error_categories"]["CONTRACT_NOT_IN_CATALOGUE"], 1)

    def test_contract_present_in_catalogue_marked_eligible_and_downloaded(self):
        """Expiry inside coverage and contract present in catalogue becomes ELIGIBLE and downloads."""
        mock_client = MagicMock()
        mock_client.test_access.return_value = {"has_token": True, "accessible": True}
        mock_client.get_expiry_coverage.return_value = {
            "underlying": "NIFTY50",
            "expiries": ["2024-10-03"],
            "earliest_expiry": "2024-10-03",
            "latest_expiry": "2024-10-03",
            "total_expiries": 1,
        }
        mock_client.get_option_contracts.return_value = [
            {
                "underlying": "NIFTY50",
                "expiry": "2024-10-03",
                "strike": 25500.0,
                "option_type": "PE",
                "instrument_key": "NSE_FO|NIFTY24O0325500PE",
                "trading_symbol": "NIFTY24O0325500PE",
                "lot_size": 25,
            }
        ]
        mock_client.fetch_and_cache_contract.return_value = (
            True,
            None,
            {
                "contract": {
                    "underlying": "NIFTY50",
                    "expiry": "2024-10-03",
                    "strike": 25500.0,
                    "option_type": "PE",
                    "instrument_key": "NSE_FO|NIFTY24O0325500PE",
                    "lot_size": 25,
                },
                "candles": [
                    {"timestamp": "2024-10-01T09:15:00", "open": 250.0, "high": 260.0, "low": 240.0, "close": 255.0, "volume": 1000},
                ],
            },
        )

        pipeline = HistoricalOptionsIngestionPipeline(cache_dir=self.cache_dir)
        pipeline.client = mock_client

        req = ContractRequirement(
            underlying="NIFTY50",
            expiry="2024-10-03",
            strike=25500.0,
            option_type="PE",
            from_date="2024-10-01",
            to_date="2024-10-03",
        )

        res = pipeline.run_ingestion([req], dry_run=False)

        self.assertEqual(req.status, "DOWNLOADED")
        self.assertEqual(req.instrument_key, "NSE_FO|NIFTY24O0325500PE")
        self.assertEqual(res["eligible_for_download"], 1)
        self.assertEqual(res["successfully_downloaded"], 1)
        self.assertEqual(res["failed_api_downloads"], 0)
        self.assertEqual(res["remaining_unavailable"], 0)

    def test_cache_resume_avoids_redownload(self):
        """Pre-existing valid cache files are detected by check_cache_status and not re-downloaded."""
        cache = OptionsDataCache(cache_dir=self.cache_dir)
        contract_info = {
            "underlying": "NIFTY50",
            "expiry": "2024-10-03",
            "strike": 25500.0,
            "option_type": "PE",
            "instrument_key": "NSE_FO|NIFTY24O0325500PE",
            "lot_size": 25,
        }
        candles = [
            {"timestamp": "2024-10-01T09:15:00", "open": 250.0, "high": 260.0, "low": 240.0, "close": 255.0, "volume": 1000}
        ]
        filename = cache.get_cache_filename("NIFTY50", "2024-10-03", 25500.0, "PE", "5minute", "2024-10-01", "2024-10-03")
        cache.save(filename, contract_info, candles)

        mock_client = MagicMock()
        mock_client.test_access.return_value = {"has_token": True, "accessible": True}

        pipeline = HistoricalOptionsIngestionPipeline(cache_dir=self.cache_dir)
        pipeline.client = mock_client

        req = ContractRequirement(
            underlying="NIFTY50",
            expiry="2024-10-03",
            strike=25500.0,
            option_type="PE",
            from_date="2024-10-01",
            to_date="2024-10-03",
        )

        res = pipeline.run_ingestion([req], dry_run=False)

        # Neither coverage, catalogue, nor candle fetch should be called
        mock_client.get_expiry_coverage.assert_not_called()
        mock_client.get_option_contracts.assert_not_called()
        mock_client.fetch_and_cache_contract.assert_not_called()

        self.assertEqual(res["strategy_required_contracts"], 1)
        self.assertEqual(res["already_cached"], 1)
        self.assertEqual(res["eligible_for_download"], 0)
        self.assertEqual(res["remaining_unavailable"], 0)

        # Cache file preserved intact
        cached_data = cache.get(filename)
        self.assertIsNotNone(cached_data)
        self.assertEqual(len(cached_data["candles"]), 1)

    def test_no_synthetic_fallback_when_contract_unavailable(self):
        """Unavailable contracts never write dummy cache files or produce synthetic data."""
        mock_client = MagicMock()
        mock_client.test_access.return_value = {"has_token": True, "accessible": True}
        mock_client.get_expiry_coverage.return_value = {
            "underlying": "NIFTY50",
            "expiries": ["2024-10-03"],
            "earliest_expiry": "2024-10-03",
            "latest_expiry": "2024-10-03",
            "total_expiries": 1,
        }

        pipeline = HistoricalOptionsIngestionPipeline(cache_dir=self.cache_dir)
        pipeline.client = mock_client

        # Expiry 2024-01-18 outside coverage
        req = ContractRequirement(
            underlying="NIFTY50",
            expiry="2024-01-18",
            strike=21700.0,
            option_type="PE",
            from_date="2024-01-15",
            to_date="2024-01-18",
        )

        res = pipeline.run_ingestion([req], dry_run=False)
        self.assertEqual(res["outside_upstox_coverage"], 1)

        # Verify no cache file exists
        cache = OptionsDataCache(cache_dir=self.cache_dir)
        filename = req.cache_filename
        self.assertIsNone(cache.get(filename))

    def test_full_pipeline_multi_tier_summary_metrics(self):
        """Pipeline computes all 8 summary metrics accurately across mixed statuses."""
        mock_client = MagicMock()
        mock_client.test_access.return_value = {"has_token": True, "accessible": True}
        mock_client.get_expiry_coverage.return_value = {
            "underlying": "NIFTY50",
            "expiries": ["2024-10-03"],
            "earliest_expiry": "2024-10-03",
            "latest_expiry": "2024-10-03",
            "total_expiries": 1,
        }
        mock_client.get_option_contracts.return_value = [
            {
                "underlying": "NIFTY50",
                "expiry": "2024-10-03",
                "strike": 25500.0,
                "option_type": "PE",
                "instrument_key": "NSE_FO|NIFTY24O0325500PE",
                "trading_symbol": "NIFTY24O0325500PE",
                "lot_size": 25,
            }
        ]
        mock_client.fetch_and_cache_contract.return_value = (
            True,
            None,
            {
                "contract": {"underlying": "NIFTY50", "expiry": "2024-10-03", "strike": 25500.0, "option_type": "PE", "instrument_key": "NSE_FO|NIFTY24O0325500PE"},
                "candles": [{"timestamp": "2024-10-01T09:15:00", "open": 200.0, "high": 210.0, "low": 190.0, "close": 205.0, "volume": 500}],
            }
        )

        pipeline = HistoricalOptionsIngestionPipeline(cache_dir=self.cache_dir)
        pipeline.client = mock_client

        # 1. Already cached contract
        cache = OptionsDataCache(cache_dir=self.cache_dir)
        cache.save(
            cache.get_cache_filename("NIFTY50", "2024-10-03", 25000.0, "CE", "5minute", "2024-10-01", "2024-10-03"),
            {"underlying": "NIFTY50", "expiry": "2024-10-03", "strike": 25000.0, "option_type": "CE", "instrument_key": "NSE_FO|NIFTY24O0325000CE"},
            [{"timestamp": "2024-10-01T09:15:00", "open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0, "volume": 500}]
        )
        req_cached = ContractRequirement("NIFTY50", "2024-10-03", 25000.0, "CE", "2024-10-01", "2024-10-03")

        # 2. Outside coverage contract
        req_outside = ContractRequirement("NIFTY50", "2024-01-18", 21700.0, "PE", "2024-01-15", "2024-01-18")

        # 3. Inside coverage, but not in catalogue
        req_not_in_cat = ContractRequirement("NIFTY50", "2024-10-03", 26000.0, "CE", "2024-10-01", "2024-10-03")

        # 4. Inside coverage, in catalogue, downloaded successfully
        req_downloaded = ContractRequirement("NIFTY50", "2024-10-03", 25500.0, "PE", "2024-10-01", "2024-10-03")

        res = pipeline.run_ingestion([req_cached, req_outside, req_not_in_cat, req_downloaded], dry_run=False)

        self.assertEqual(res["strategy_required_contracts"], 4)
        self.assertEqual(res["already_cached"], 1)
        self.assertEqual(res["outside_upstox_coverage"], 1)
        self.assertEqual(res["not_present_in_catalogue"], 1)
        self.assertEqual(res["eligible_for_download"], 1)
        self.assertEqual(res["successfully_downloaded"], 1)
        self.assertEqual(res["failed_api_downloads"], 0)
        self.assertEqual(res["remaining_unavailable"], 2)  # 1 outside coverage + 1 not in catalogue

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
