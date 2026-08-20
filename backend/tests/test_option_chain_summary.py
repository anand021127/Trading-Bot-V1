"""Tests for backend.market_data.option_chain — PCR, Max Pain, and OI
buildup classification (Long Build-up, Short Build-up, Short Covering,
Long Unwinding), the last of which was explicitly requested but entirely
missing before this session."""
from __future__ import annotations

import pytest

from backend.market_data.option_chain import (
    classify_buildup,
    summarize_chain,
    LONG_BUILDUP,
    SHORT_BUILDUP,
    SHORT_COVERING,
    LONG_UNWINDING,
    NEUTRAL_BUILDUP,
)


class TestClassifyBuildup:
    def test_price_up_oi_up_is_long_buildup(self) -> None:
        assert classify_buildup(price_change=5.0, oi_change=1000) == LONG_BUILDUP

    def test_price_down_oi_up_is_short_buildup(self) -> None:
        assert classify_buildup(price_change=-5.0, oi_change=1000) == SHORT_BUILDUP

    def test_price_up_oi_down_is_short_covering(self) -> None:
        assert classify_buildup(price_change=5.0, oi_change=-1000) == SHORT_COVERING

    def test_price_down_oi_down_is_long_unwinding(self) -> None:
        assert classify_buildup(price_change=-5.0, oi_change=-1000) == LONG_UNWINDING

    def test_missing_price_change_is_neutral_not_a_guess(self) -> None:
        assert classify_buildup(price_change=None, oi_change=1000) == NEUTRAL_BUILDUP

    def test_missing_oi_change_is_neutral_not_a_guess(self) -> None:
        assert classify_buildup(price_change=5.0, oi_change=None) == NEUTRAL_BUILDUP

    def test_exactly_flat_is_neutral(self) -> None:
        assert classify_buildup(price_change=0.0, oi_change=1000) == NEUTRAL_BUILDUP
        assert classify_buildup(price_change=5.0, oi_change=0.0) == NEUTRAL_BUILDUP


SAMPLE_CHAIN = [
    {"strike": 22000, "option_type": "CE", "oi": 50000, "oi_change": 5000,
     "ltp": 120.0, "close_price": 110.0},   # price up, OI up -> LONG_BUILDUP
    {"strike": 22000, "option_type": "PE", "oi": 30000, "oi_change": -2000,
     "ltp": 80.0, "close_price": 75.0},     # price up, OI down -> SHORT_COVERING
    {"strike": 22100, "option_type": "CE", "oi": 70000, "oi_change": -3000,
     "ltp": 90.0, "close_price": 100.0},    # price down, OI down -> LONG_UNWINDING
    {"strike": 22100, "option_type": "PE", "oi": 60000, "oi_change": 4000,
     "ltp": 60.0, "close_price": 65.0},     # price down, OI up -> SHORT_BUILDUP
]


class TestSummarizeChainBuildups:
    def test_strike_buildups_present_for_every_strike(self) -> None:
        summary = summarize_chain("NIFTY50", "2026-02-26", SAMPLE_CHAIN, spot=22050)
        strikes = {row["strike"] for row in summary.strike_buildups}
        assert strikes == {22000.0, 22100.0}

    def test_call_and_put_buildup_classified_independently_per_strike(self) -> None:
        summary = summarize_chain("NIFTY50", "2026-02-26", SAMPLE_CHAIN, spot=22050)
        row_22000 = next(r for r in summary.strike_buildups if r["strike"] == 22000.0)
        row_22100 = next(r for r in summary.strike_buildups if r["strike"] == 22100.0)
        assert row_22000["call_buildup"] == LONG_BUILDUP
        assert row_22000["put_buildup"] == SHORT_COVERING
        assert row_22100["call_buildup"] == LONG_UNWINDING
        assert row_22100["put_buildup"] == SHORT_BUILDUP

    def test_pcr_and_max_pain_still_computed_correctly(self) -> None:
        summary = summarize_chain("NIFTY50", "2026-02-26", SAMPLE_CHAIN, spot=22050)
        assert summary.pcr == pytest.approx((30000 + 60000) / (50000 + 70000))
        assert summary.max_pain is not None

    def test_unsupported_underlying_raises(self) -> None:
        with pytest.raises(ValueError):
            summarize_chain("RELIANCE", "2026-02-26", SAMPLE_CHAIN, spot=100)

    def test_empty_chain_returns_none_metrics_not_fabricated(self) -> None:
        summary = summarize_chain("NIFTY50", "2026-02-26", [], spot=22050)
        assert summary.pcr is None
        assert summary.max_pain is None
        assert summary.strike_buildups == []
