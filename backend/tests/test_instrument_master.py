"""Tests for the dynamic instrument master resolver.

This is the fix for a real production bug: a hardcoded ISIN went stale
after a corporate action (KOTAKBANK's face-value sub-division), causing
"Invalid Instrument key" errors. These tests verify the live-master-first,
static-fallback-second resolution order, and that a failed refresh never
breaks anything — it just falls back.
"""
from __future__ import annotations

import gzip
import io
import json
from unittest.mock import MagicMock, patch

from backend.broker.instrument_master import InstrumentMaster, resolve_instrument_key


def _gzipped_json(rows: list) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(json.dumps(rows).encode("utf-8"))
    return buf.getvalue()


def _fake_response(rows: list) -> MagicMock:
    resp = MagicMock()
    resp.content = _gzipped_json(rows)
    resp.raise_for_status = MagicMock()
    return resp


SAMPLE_ROWS = [
    {"segment": "NSE_EQ", "trading_symbol": "KOTAKBANK", "instrument_key": "NSE_EQ|INE_NEW_KEY_2026"},
    {"segment": "NSE_EQ", "trading_symbol": "RELIANCE", "instrument_key": "NSE_EQ|INE002A01018"},
    {"segment": "NSE_INDEX", "trading_symbol": "NIFTY 50", "instrument_key": "NSE_INDEX|Nifty 50"},
    {"segment": "NSE_FO", "trading_symbol": "NIFTY26FEBFUT", "instrument_key": "NSE_FO|IGNORED"},
]


class TestInstrumentMaster:
    def test_fetches_and_resolves_current_instrument_key(self) -> None:
        master = InstrumentMaster()
        with patch("requests.get", return_value=_fake_response(SAMPLE_ROWS)):
            key = master.resolve("KOTAKBANK")
        assert key == "NSE_EQ|INE_NEW_KEY_2026"

    def test_filters_out_irrelevant_segments(self) -> None:
        master = InstrumentMaster()
        with patch("requests.get", return_value=_fake_response(SAMPLE_ROWS)):
            master.ensure_fresh()
        assert master.resolve("NIFTY26FEBFUT") is None  # NSE_FO row excluded

    def test_unknown_symbol_returns_none_not_a_guess(self) -> None:
        master = InstrumentMaster()
        with patch("requests.get", return_value=_fake_response(SAMPLE_ROWS)):
            assert master.resolve("TOTALLY_UNKNOWN_SYMBOL") is None

    def test_fetch_failure_does_not_raise(self) -> None:
        master = InstrumentMaster()
        with patch("requests.get", side_effect=ConnectionError("network down")):
            master.ensure_fresh()  # must not raise
        status = master.status()
        assert status["last_error"] is not None
        assert status["symbols_loaded"] == 0

    def test_caches_within_ttl_does_not_refetch(self) -> None:
        master = InstrumentMaster()
        with patch("requests.get", return_value=_fake_response(SAMPLE_ROWS)) as mock_get:
            master.resolve("RELIANCE")
            master.resolve("RELIANCE")
            master.resolve("KOTAKBANK")
        assert mock_get.call_count == 1  # only fetched once across 3 calls

    def test_refetches_after_ttl_expires(self) -> None:
        import time
        from backend.broker.instrument_master import REFRESH_TTL_SECONDS
        master = InstrumentMaster()
        with patch("requests.get", return_value=_fake_response(SAMPLE_ROWS)) as mock_get:
            master.resolve("RELIANCE")
            master._fetched_at = time.monotonic() - REFRESH_TTL_SECONDS - 1  # force staleness
            master.resolve("RELIANCE")
        assert mock_get.call_count == 2

    def test_status_reports_symbol_count_and_freshness(self) -> None:
        master = InstrumentMaster()
        with patch("requests.get", return_value=_fake_response(SAMPLE_ROWS)):
            master.ensure_fresh()
        status = master.status()
        assert status["symbols_loaded"] == 3  # 3 relevant rows (NSE_FO excluded)
        assert status["is_stale"] is False
        assert status["last_error"] is None


class TestResolveInstrumentKeyHelper:
    def test_prefers_live_master_over_static_fallback(self) -> None:
        with patch("backend.broker.instrument_master._instrument_master.resolve",
                   return_value="NSE_EQ|LIVE_KEY"):
            result = resolve_instrument_key("KOTAKBANK", static_fallback="NSE_EQ|STALE_KEY")
        assert result == "NSE_EQ|LIVE_KEY"

    def test_falls_back_to_static_when_live_master_misses(self) -> None:
        with patch("backend.broker.instrument_master._instrument_master.resolve", return_value=None):
            result = resolve_instrument_key("KOTAKBANK", static_fallback="NSE_EQ|STATIC_KEY")
        assert result == "NSE_EQ|STATIC_KEY"

    def test_returns_none_when_neither_source_has_it(self) -> None:
        with patch("backend.broker.instrument_master._instrument_master.resolve", return_value=None):
            result = resolve_instrument_key("NOPE", static_fallback=None)
        assert result is None
