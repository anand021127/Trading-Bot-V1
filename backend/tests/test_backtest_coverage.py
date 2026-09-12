"""Tests for BacktestResult's coverage-validation fields (this session's
fix for: "the previous 1-year backtest reported a full-year date range
but actual trades were concentrated in only a small portion of the
year").

Follows this repo's unittest.TestCase convention (see
test_multi_symbol_backtest.py) so these are picked up by both
run_all_tests.py and the pytest shim.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from backend.backtest.engine import BacktestEngine


def _candles_for_dates(dates, start_price=20000.0):
    """One trading day's worth of 5-min candles (75 bars, 09:15-15:20)
    for each date in `dates`."""
    candles = []
    price = start_price
    for d in dates:
        base = datetime.fromisoformat(f"{d}T09:15:00")
        for i in range(75):
            ts = (base + timedelta(minutes=5 * i)).isoformat()
            price += 1.0
            candles.append({
                "timestamp": ts, "open": price, "high": price + 5, "low": price - 5,
                "close": price + 1, "volume": 1000,
            })
    return candles


class TestBacktestCoverageValidation(unittest.TestCase):

    def setUp(self):
        self.engine = BacktestEngine(min_candles_required=20)

    def test_full_requested_range_reports_complete(self):
        # Every weekday in a 2-week window has data -> should be ~100% coverage.
        dates = []
        d = datetime(2024, 1, 1)
        while d <= datetime(2024, 1, 12):
            if d.weekday() < 5:
                dates.append(d.date().isoformat())
            d += timedelta(days=1)

        candles = _candles_for_dates(dates)
        result = self.engine.run(
            {"NIFTY50": candles},
            requested_start_date="2024-01-01", requested_end_date="2024-01-12",
        )
        self.assertEqual(result.coverage_status, "COMPLETE")
        self.assertGreaterEqual(result.data_coverage_pct, 80.0)
        self.assertEqual(result.trading_days_missing, 0)

    def test_large_gap_reports_failed_incomplete_coverage(self):
        """THE reported bug: a backtest 'requested' a full range but only
        a small fraction of it actually had candle data — must be
        flagged FAILED_INCOMPLETE_COVERAGE, not silently presented as a
        successful full-range test."""
        # Only 3 trading days of data out of a full year requested.
        dates = ["2025-09-10", "2025-09-11", "2025-09-12"]
        candles = _candles_for_dates(dates)
        result = self.engine.run(
            {"NIFTY50": candles},
            requested_start_date="2025-09-10", requested_end_date="2026-09-11",
        )
        self.assertEqual(result.coverage_status, "FAILED_INCOMPLETE_COVERAGE")
        self.assertLess(result.data_coverage_pct, 80.0)
        self.assertGreater(result.trading_days_missing, 0)
        self.assertIn("2025-09-10", result.coverage_notes)  # explains itself, not a bare flag

    def test_actual_data_start_end_reported_even_when_narrower_than_requested(self):
        dates = ["2024-03-04", "2024-03-05", "2024-03-06"]
        candles = _candles_for_dates(dates)
        result = self.engine.run(
            {"NIFTY50": candles},
            requested_start_date="2024-01-01", requested_end_date="2024-12-31",
        )
        self.assertEqual(result.actual_data_start_date, "2024-03-04")
        self.assertEqual(result.actual_data_end_date, "2024-03-06")
        # Actual data range must be visible independent of the coverage
        # verdict — this is what lets a human see exactly where the gap is.
        self.assertNotEqual(result.actual_data_start_date, result.requested_start_date)

    def test_no_requested_range_given_is_honestly_unknown_not_complete(self):
        """Without a requested range to compare against, coverage must
        NOT default to claiming success — that would recreate exactly
        the silent-success problem this fix exists to prevent."""
        candles = _candles_for_dates(["2024-01-02"])
        result = self.engine.run({"NIFTY50": candles})
        self.assertEqual(result.coverage_status, "UNKNOWN")

    def test_coverage_threshold_is_configurable(self):
        dates = ["2024-01-02", "2024-01-03"]  # 2 of 5 weekdays in the requested window
        candles = _candles_for_dates(dates)
        result_strict = self.engine.run(
            {"NIFTY50": candles},
            requested_start_date="2024-01-01", requested_end_date="2024-01-05",
            min_coverage_pct=90.0,
        )
        result_lenient = self.engine.run(
            {"NIFTY50": candles},
            requested_start_date="2024-01-01", requested_end_date="2024-01-05",
            min_coverage_pct=20.0,
        )
        self.assertEqual(result_strict.coverage_status, "FAILED_INCOMPLETE_COVERAGE")
        self.assertEqual(result_lenient.coverage_status, "COMPLETE")

    def test_coverage_approximation_never_rounds_favorably(self):
        """Weekday-count approximation ignores exchange holidays, which
        can only ever make the DENOMINATOR (requested trading days) an
        overestimate relative to real trading days — meaning reported
        coverage % is a conservative floor, never inflated."""
        # A week with a known holiday-heavy stretch would show slightly
        # LOWER than true coverage under this approximation, which is
        # the safe direction to be wrong in.
        dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
        candles = _candles_for_dates(dates)
        result = self.engine.run(
            {"NIFTY50": candles},
            requested_start_date="2024-01-01", requested_end_date="2024-01-05",
        )
        # 4 weekdays present out of 5 weekdays in range (Jan 1 is a Monday holiday
        # in reality, but this approximation still counts it as a requested
        # trading day) -> coverage is 80%, not artificially higher.
        self.assertEqual(result.trading_days_requested, 5)
        self.assertAlmostEqual(result.data_coverage_pct, 80.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
