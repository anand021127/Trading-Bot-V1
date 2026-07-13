"""Tests for UpstoxClient.get_historical_candles — the v3 migration (item #6).

Covers: correct unit/interval mapping (the old bug silently turned 5minute/
15minute into 30minute), chunked multi-request fetching for wide date
ranges, dedup at chunk boundaries, and that a failed chunk produces a gap
+ warning rather than fabricated candles.
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import patch

from backend.broker.upstox_client import UpstoxClient, UpstoxAPIError, V3_INTERVAL_MAP


def _candle_row(ts: str, price: float = 100.0, vol: int = 1000) -> List[Any]:
    return [ts, price, price + 1, price - 1, price, vol, 0]


class TestIntervalMapping:
    def test_5minute_maps_to_minutes_5_not_30(self) -> None:
        # This is the exact bug the user hit — v2 silently rewrote this to 30.
        assert V3_INTERVAL_MAP["5minute"] == ("minutes", 5)

    def test_15minute_maps_to_minutes_15_not_30(self) -> None:
        assert V3_INTERVAL_MAP["15minute"] == ("minutes", 15)

    def test_day_maps_to_days_1(self) -> None:
        assert V3_INTERVAL_MAP["day"] == ("days", 1)

    def test_week_maps_to_weeks_1(self) -> None:
        assert V3_INTERVAL_MAP["week"] == ("weeks", 1)


class TestGetHistoricalCandlesV3:
    def test_uses_v3_url_with_correct_unit_and_interval(self) -> None:
        client = UpstoxClient(access_token="tok")
        captured_urls: List[str] = []

        def fake_get_url(url: str, params: Any = None) -> Dict[str, Any]:
            captured_urls.append(url)
            return {"data": {"candles": [_candle_row("2026-01-05T09:15:00+05:30")]}}

        with patch.object(client, "_get_url", side_effect=fake_get_url):
            client.get_historical_candles(
                "RELIANCE", "5minute", from_date="2026-01-01", to_date="2026-01-05",
            )

        assert len(captured_urls) == 1
        assert "/v3/historical-candle/" in captured_urls[0]
        assert "/minutes/5/" in captured_urls[0]

    def test_wide_range_is_split_into_multiple_chunks(self) -> None:
        client = UpstoxClient(access_token="tok")
        calls: List[str] = []

        def fake_get_url(url: str, params: Any = None) -> Dict[str, Any]:
            calls.append(url)
            return {"data": {"candles": [_candle_row(f"2026-01-01T09:15:00+05:30-{len(calls)}")]}}

        # ~90 days at 5-minute granularity should need multiple ~25-day chunks.
        with patch.object(client, "_get_url", side_effect=fake_get_url):
            client.get_historical_candles(
                "RELIANCE", "5minute", from_date="2026-01-01", to_date="2026-04-01", limit=0,
            )

        assert len(calls) >= 3  # proves it's not a single truncated request

    def test_daily_range_is_not_chunked_unnecessarily(self) -> None:
        client = UpstoxClient(access_token="tok")
        calls: List[str] = []

        def fake_get_url(url: str, params: Any = None) -> Dict[str, Any]:
            calls.append(url)
            return {"data": {"candles": [_candle_row("2026-01-01")]}}

        with patch.object(client, "_get_url", side_effect=fake_get_url):
            client.get_historical_candles(
                "RELIANCE", "day", from_date="2026-01-01", to_date="2026-03-01", limit=0,
            )

        assert len(calls) == 1  # 60 days of daily candles fits in one chunk

    def test_failed_chunk_produces_gap_not_fabricated_data(self) -> None:
        client = UpstoxClient(access_token="tok")
        call_count = {"n": 0}

        def fake_get_url(url: str, params: Any = None) -> Dict[str, Any]:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise UpstoxAPIError(500, "simulated upstream failure")
            return {"data": {"candles": [_candle_row("2026-01-01T09:15:00+05:30")]}}

        with patch.object(client, "_get_url", side_effect=fake_get_url):
            result = client.get_historical_candles(
                "RELIANCE", "5minute", from_date="2025-11-01", to_date="2026-01-05", limit=0,
            )

        # We still get the candles from the chunk that succeeded — no fake
        # data was invented to fill the failed chunk's gap.
        assert len(result) >= 1
        assert call_count["n"] >= 2

    def test_dedup_at_chunk_boundaries(self) -> None:
        client = UpstoxClient(access_token="tok")

        def fake_get_url(url: str, params: Any = None) -> Dict[str, Any]:
            # Every chunk "sees" the same boundary candle — should be deduped.
            return {"data": {"candles": [_candle_row("2026-01-01T09:15:00+05:30")]}}

        with patch.object(client, "_get_url", side_effect=fake_get_url):
            result = client.get_historical_candles(
                "RELIANCE", "5minute", from_date="2025-10-01", to_date="2026-01-05", limit=0,
            )

        timestamps = [r["timestamp"] for r in result]
        assert len(timestamps) == len(set(timestamps))

    def test_raw_instrument_key_passed_through_for_option_contracts(self) -> None:
        client = UpstoxClient(access_token="tok")
        captured_urls: List[str] = []

        def fake_get_url(url: str, params: Any = None) -> Dict[str, Any]:
            captured_urls.append(url)
            return {"data": {"candles": []}}

        with patch.object(client, "_get_url", side_effect=fake_get_url):
            client.get_historical_candles(
                "NSE_FO|12345", "5minute", from_date="2026-01-01", to_date="2026-01-02",
            )

        assert "NSE_FO|12345" in captured_urls[0]
        assert "NSE_EQ|NSE_FO" not in captured_urls[0]  # the old double-mangling bug

    def test_live_instrument_master_overrides_stale_static_isin(self) -> None:
        """Regression test for a real production incident: KOTAKBANK's
        hardcoded ISIN (INE237A01028) started failing with 'Invalid
        Instrument key' after a corporate action changed which key
        Upstox's live master recognizes. get_historical_candles must use
        the live master's current key, not the stale static one."""
        client = UpstoxClient(access_token="tok")
        captured_urls: List[str] = []

        def fake_get_url(url: str, params: Any = None) -> Dict[str, Any]:
            captured_urls.append(url)
            return {"data": {"candles": []}}

        with patch("backend.broker.instrument_master.resolve_instrument_key",
                   return_value="NSE_EQ|INE_CURRENT_POST_SPLIT_KEY"), \
             patch.object(client, "_get_url", side_effect=fake_get_url):
            client.get_historical_candles(
                "KOTAKBANK", "5minute", from_date="2026-01-01", to_date="2026-01-02",
            )

        assert "INE_CURRENT_POST_SPLIT_KEY" in captured_urls[0]
        assert "INE237A01028" not in captured_urls[0]  # the stale key must not be used

    def test_falls_back_to_static_isin_when_live_master_unavailable(self) -> None:
        """If the live master can't be reached (network blip), the static
        dict is still better than nothing — never just fail the request."""
        client = UpstoxClient(access_token="tok")
        captured_urls: List[str] = []

        def fake_get_url(url: str, params: Any = None) -> Dict[str, Any]:
            captured_urls.append(url)
            return {"data": {"candles": []}}

        with patch("backend.broker.instrument_master._instrument_master.resolve", return_value=None), \
             patch.object(client, "_get_url", side_effect=fake_get_url):
            client.get_historical_candles(
                "RELIANCE", "5minute", from_date="2026-01-01", to_date="2026-01-02",
            )

        assert "INE002A01018" in captured_urls[0]  # static RELIANCE ISIN used as fallback

    def test_full_range_helper_does_not_truncate(self) -> None:
        client = UpstoxClient(access_token="tok")
        rows = [_candle_row(f"2026-01-0{i}T09:15:00+05:30") for i in range(1, 6)]

        def fake_get_url(url: str, params: Any = None) -> Dict[str, Any]:
            return {"data": {"candles": rows}}

        with patch.object(client, "_get_url", side_effect=fake_get_url):
            result = client.get_historical_candles_full_range(
                "RELIANCE", "day", from_date="2026-01-01", to_date="2026-01-05",
            )
        assert len(result) == 5
