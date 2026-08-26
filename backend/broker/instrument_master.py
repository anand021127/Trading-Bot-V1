"""Upstox instrument master resolver.

Upstox publishes a daily-refreshed instrument master file precisely
because instrument keys genuinely change — corporate actions (splits,
mergers, relistings) can retire an old ISIN/instrument_key and issue a new
one. A hardcoded symbol->ISIN dict WILL go stale; this is what actually
happened in production (KOTAKBANK started failing with "Invalid Instrument
key" after a face-value sub-division changed which ISIN Upstox's live
instrument master recognizes).

This module fetches the real, current instrument master and resolves
trading_symbol -> instrument_key dynamically, with the old static dict
kept ONLY as a last-resort fallback if the live master can't be fetched
(e.g. no network, Upstox's asset CDN briefly down) — so a symbol lookup
never just breaks, but it also never silently trusts stale data when a
fresh master is available.

Source: https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz
Refreshed by Upstox daily at ~6 AM IST; we mirror that with a 24h TTL.
"""
from __future__ import annotations

import gzip
import io
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

try:
    import requests
except ImportError:
    requests = None

logger = logging.getLogger(__name__)

INSTRUMENT_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
REFRESH_TTL_SECONDS = 24 * 60 * 60  # matches Upstox's own daily refresh cadence

# Segments/instrument_types we actually need — filtering these out of the
# full multi-exchange file (which also includes F&O, MCX, etc.) keeps the
# in-memory lookup small.
_RELEVANT_SEGMENTS = {"NSE_INDEX", "BSE_INDEX", "NSE_FO", "BSE_FO"}


class InstrumentMaster:
    """Lazily-fetched, TTL-cached trading_symbol -> instrument_key lookup."""

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout
        self._symbol_to_key: Dict[str, str] = {}
        self._key_to_metadata: Dict[str, Dict[str, Any]] = {}
        self._fetched_at: float = 0.0
        self._last_error: Optional[str] = None

    def _is_stale(self) -> bool:
        return (time.monotonic() - self._fetched_at) > REFRESH_TTL_SECONDS

    def _fetch_and_parse(self) -> Dict[str, str]:
        if requests is not None:
            resp = requests.get(INSTRUMENT_MASTER_URL, timeout=self.timeout)
            resp.raise_for_status()
            content = resp.content
        else:
            req = urllib.request.Request(
                INSTRUMENT_MASTER_URL,
                headers={"User-Agent": "Mozilla/5.0 (compatible; InstrumentMaster/1.0)"},
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                content = resp.read()

        with gzip.GzipFile(fileobj=io.BytesIO(content)) as gz:
            data: Any = json.load(gz)

        mapping: Dict[str, str] = {}
        for row in data:
            segment = row.get("segment")
            if segment not in _RELEVANT_SEGMENTS:
                continue
            symbol = row.get("trading_symbol")
            key = row.get("instrument_key")
            if symbol and key:
                mapping[symbol.upper()] = key
                self._key_to_metadata[key] = {
                    "lot_size": row.get("lot_size"),
                    "freeze_quantity": row.get("freeze_quantity"),
                }
        return mapping

    def ensure_fresh(self) -> None:
        """Refresh if stale or never loaded. Never raises — a failed
        refresh just means resolve() falls back to the static dict, logged
        once rather than breaking every request."""
        if self._symbol_to_key and not self._is_stale():
            return
        try:
            fresh = self._fetch_and_parse()
            if fresh:
                self._symbol_to_key = fresh
                self._fetched_at = time.monotonic()
                self._last_error = None
                logger.info("Instrument master refreshed — %d symbols", len(fresh))
            else:
                self._last_error = "Fetched master was empty"
        except Exception as e:
            self._last_error = str(e)
            logger.warning(
                "Could not refresh Upstox instrument master (%s) — "
                "using static fallback mapping until next retry", e,
            )

    def resolve(self, symbol: str) -> Optional[str]:
        """Current instrument_key for `symbol`, or None if not found even
        in the live master (caller decides whether to fall back)."""
        self.ensure_fresh()
        return self._symbol_to_key.get(symbol.upper())

    def status(self) -> Dict[str, Any]:
        return {
            "symbols_loaded": len(self._symbol_to_key),
            "last_refreshed_seconds_ago": (
                round(time.monotonic() - self._fetched_at, 1) if self._fetched_at else None
            ),
            "is_stale": self._is_stale() if self._fetched_at else True,
            "last_error": self._last_error,
        }

    def metadata_for_key(self, instrument_key: str) -> Dict[str, Any]:
        self.ensure_fresh()
        return dict(self._key_to_metadata.get(instrument_key, {}))


# Module-level singleton — one shared cache per process, same pattern as
# the rest of this codebase's client/engine singletons.
_instrument_master = InstrumentMaster()


def resolve_instrument_key(symbol: str, static_fallback: Optional[str] = None) -> Optional[str]:
    """Resolve `symbol` to its CURRENT Upstox instrument_key, preferring
    the live daily-refreshed master. Falls back to `static_fallback` (the
    old hardcoded dict's value) only if the live master doesn't have it —
    never the other way around, since the live master is more likely to be
    correct after a corporate action."""
    live = _instrument_master.resolve(symbol)
    if live:
        return live
    return static_fallback


def get_master_status() -> Dict[str, Any]:
    return _instrument_master.status()


def get_instrument_metadata(instrument_key: str) -> Dict[str, Any]:
    return _instrument_master.metadata_for_key(instrument_key)


def force_refresh() -> Dict[str, Any]:
    """For a manual /diagnostics or admin trigger — bypasses the TTL."""
    _instrument_master._fetched_at = 0.0
    _instrument_master.ensure_fresh()
    return _instrument_master.status()
