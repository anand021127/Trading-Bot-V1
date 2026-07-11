"""Upstox v3 Market Data Feed WebSocket client.

The v2 feed (`/v2/feed/market-data-feed`) was discontinued by Upstox
(HTTP 410). This module implements the v3 feed exclusively.

Why we use the official `upstox-python-sdk` instead of hand-rolling the
protobuf decode:
  - The v3 feed is protobuf-only (binary frames). Upstox does not publish
    a stable public .proto file for every SDK release, and several
    developers have hit "duplicate symbol" / "heartbeat only, no ticks"
    errors trying to compile it themselves (see Upstox community forum).
  - The SDK (`upstox_client.MarketDataStreamerV3`) bundles a compiled,
    version-matched `MarketDataFeedV3_pb2` module and connects directly to
    `wss://api.upstox.com/v3/feed/market-data-feed` with the access token
    in the `Authorization` header — no separate `/authorize` redirect hop
    needed for v3.
  - It ships its own reconnect/backoff state machine (open/close/error/
    reconnecting events), which we hook into for status + logging.

No mock/synthetic prices are ever produced here. If the token is missing,
invalid, or the feed is down, `get_latest_prices()` simply stays empty and
`connection_status` reports the real state — callers must render that
honestly rather than fabricate numbers.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

V3_FEED_URL = "wss://api.upstox.com/v3/feed/market-data-feed"


def _extract_ltpc(feed: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the LTPC (last-traded-price-close) block out of a decoded v3 feed
    entry, regardless of which sub-message it arrived in (ltpc / fullFeed.
    marketFF / fullFeed.indexFF / firstLevelWithGreeks).
    """
    if "ltpc" in feed:
        return feed.get("ltpc") or {}
    full = feed.get("fullFeed") or {}
    market_ff = full.get("marketFF")
    index_ff = full.get("indexFF")
    if market_ff:
        return market_ff.get("ltpc") or {}
    if index_ff:
        return index_ff.get("ltpc") or {}
    flwg = feed.get("firstLevelWithGreeks") or {}
    if flwg:
        return flwg.get("ltpc") or {}
    return {}


def _extract_volume(feed: Dict[str, Any]) -> int:
    """Volume traded today (`vtt`). Only present for equities/futures — index
    feeds (NIFTY 50, BANKNIFTY, SENSEX) have no traded volume, so this
    correctly returns 0 for them rather than guessing."""
    full = feed.get("fullFeed") or {}
    market_ff = full.get("marketFF")
    if market_ff and "vtt" in market_ff:
        try:
            return int(float(market_ff["vtt"]))
        except (TypeError, ValueError):
            return 0
    return 0


class UpstoxWebSocketClient:
    """Thin wrapper around `upstox_client.MarketDataStreamerV3` that exposes:

      - `start()` / `stop()`               — lifecycle
      - `subscribe(instrument_keys)`        — add instruments to watch
      - `get_latest_prices()` / `get_price(key)` — read the live tick cache
      - `connection_status`                 — 'disconnected' | 'connecting' |
                                               'connected' | 'reconnecting' |
                                               'auth_failed'
      - `is_data_stale(max_age_seconds)`    — staleness check for the UI
    """

    def __init__(
        self,
        access_token: Optional[str] = None,
        instrument_keys: Optional[List[str]] = None,
        on_price_update: Optional[Callable[[Dict[str, Any]], None]] = None,
        mode: str = "full",
    ) -> None:
        self.access_token = access_token or os.getenv("UPSTOX_ACCESS_TOKEN", "")
        self._instrument_keys: List[str] = list(instrument_keys or [])
        self._on_price_update = on_price_update
        self.mode = mode

        self._prices: Dict[str, Any] = {}
        self._prices_lock = threading.Lock()

        self.connection_status = "disconnected"
        self.is_connected = False
        self._last_message_time: float = 0.0
        self._last_message_wall: Optional[datetime] = None
        self._connected_since: Optional[datetime] = None
        self._total_ticks = 0
        self._last_error: Optional[str] = None
        self._reconnect_attempts = 0

        self._streamer: Any = None
        self._should_run = False

    # ── public API ──────────────────────────────────────────────────────

    def subscribe(self, instrument_keys: List[str]) -> None:
        new_keys = [k for k in instrument_keys if k not in self._instrument_keys]
        self._instrument_keys.extend(new_keys)
        if self._streamer is not None and self.is_connected and new_keys:
            try:
                self._streamer.subscribe(new_keys, self.mode)
                logger.info("WS subscribed to %d new instrument keys", len(new_keys))
            except Exception as e:
                logger.warning("WS subscribe failed: %s", e)

    def get_latest_prices(self) -> Dict[str, Any]:
        with self._prices_lock:
            return dict(self._prices)

    def get_price(self, instrument_key: str) -> Optional[Dict[str, Any]]:
        with self._prices_lock:
            return self._prices.get(instrument_key)

    def is_data_stale(self, max_age_seconds: float = 30.0) -> bool:
        if self._last_message_time == 0:
            return True
        return time.monotonic() - self._last_message_time > max_age_seconds

    def status_report(self) -> Dict[str, Any]:
        """Everything the diagnostics/dashboard UI needs to show honestly."""
        return {
            "connection_status": self.connection_status,
            "is_connected": self.is_connected,
            "subscribed_instruments": len(self._instrument_keys),
            "last_tick_age_seconds": (
                round(time.monotonic() - self._last_message_time, 1)
                if self._last_message_time else None
            ),
            # Wall-clock ISO of the most recent tick (what the dashboard shows as
            # "last websocket tick time"). None until the first tick arrives.
            "last_tick_time": (
                self._last_message_wall.isoformat() if self._last_message_wall else None
            ),
            "connected_since": (
                self._connected_since.isoformat() if self._connected_since else None
            ),
            "total_ticks": self._total_ticks,
            "is_stale": self.is_data_stale(),
            "last_error": self._last_error,
            "reconnect_attempts": self._reconnect_attempts,
            "feed_endpoint": V3_FEED_URL,
            "feed_version": "v3",
        }

    def start(self) -> None:
        if not self.access_token:
            self.connection_status = "auth_failed"
            self._last_error = "No Upstox access token configured"
            logger.warning("WebSocket not started — no access token")
            return
        if self._should_run:
            logger.debug("WebSocket already running")
            return

        try:
            import upstox_client  # noqa: F401
        except ImportError:
            self.connection_status = "disconnected"
            self._last_error = "upstox-python-sdk not installed"
            logger.error(
                "upstox-python-sdk is not installed. "
                "Run: pip install upstox-python-sdk websocket-client"
            )
            return

        self._should_run = True
        self.connection_status = "connecting"
        logger.info(
            "Starting Upstox v3 WebSocket client — %d instruments, mode=%s",
            len(self._instrument_keys), self.mode,
        )
        self._build_and_connect()

    def stop(self) -> None:
        self._should_run = False
        if self._streamer is not None:
            try:
                self._streamer.auto_reconnect(False)
                self._streamer.disconnect()
                logger.info("WebSocket disconnected cleanly")
            except Exception as e:
                logger.debug("WS disconnect error (ignored): %s", e)
        self.is_connected = False
        self.connection_status = "disconnected"

    # ── internals ────────────────────────────────────────────────────────

    def _build_and_connect(self) -> None:
        import upstox_client

        configuration = upstox_client.Configuration()
        configuration.access_token = self.access_token
        api_client = upstox_client.ApiClient(configuration)

        streamer = upstox_client.MarketDataStreamerV3(
            api_client, self._instrument_keys, self.mode,
        )
        # Keep retrying — the market can be closed for hours; we still want
        # the socket to come back the instant it's reachable again.
        streamer.auto_reconnect(True, interval=3, retry_count=100000)

        streamer.on("open", self._on_open)
        streamer.on("message", self._on_message)
        streamer.on("error", self._on_error)
        streamer.on("close", self._on_close)
        streamer.on("reconnecting", self._on_reconnecting)
        streamer.on("autoReconnectStopped", self._on_reconnect_stopped)

        self._streamer = streamer
        streamer.connect()  # non-blocking — SDK runs the socket in a thread

    def _on_open(self, *_args: Any) -> None:
        self.is_connected = True
        self.connection_status = "connected"
        self._connected_since = datetime.now(timezone.utc)
        self._reconnect_attempts = 0
        self._last_error = None
        logger.info(
            "Upstox v3 WebSocket CONNECTED (%s) — %d instruments subscribed, mode=%s",
            V3_FEED_URL, len(self._instrument_keys), self.mode,
        )
        # Re-assert the subscription on (re)connect. The SDK subscribes the
        # constructor keys itself, but doing it again is idempotent and covers
        # the reconnect case where the server dropped our subscription.
        if self._streamer is not None and self._instrument_keys:
            try:
                self._streamer.subscribe(list(self._instrument_keys), self.mode)
                logger.info("WS (re)subscribed %d instruments on open",
                            len(self._instrument_keys))
            except Exception as e:
                logger.debug("WS subscribe-on-open skipped: %s", e)

    def _on_message(self, _ws: Any, data: Dict[str, Any]) -> None:
        """`data` is the already protobuf-decoded FeedResponse as a dict:
        {"type": "...", "feeds": {instrument_key: {...}}, "currentTs": "..."}
        """
        self._last_message_time = time.monotonic()
        self._last_message_wall = datetime.now(timezone.utc)
        self._total_ticks += 1
        msg_type = data.get("type")
        if msg_type == "market_info":
            logger.debug("WS market_info tick: %s", data.get("marketInfo"))
            return

        feeds = data.get("feeds") or {}
        if not feeds:
            return

        updated: List[str] = []
        with self._prices_lock:
            for instrument_key, feed in feeds.items():
                ltpc = _extract_ltpc(feed)
                if not ltpc or "ltp" not in ltpc:
                    continue
                try:
                    ltp = float(ltpc.get("ltp", 0) or 0)
                    cp = float(ltpc.get("cp", 0) or 0)
                except (TypeError, ValueError):
                    continue
                change = ltp - cp if cp else 0.0
                change_pct = (change / cp * 100.0) if cp else 0.0
                trend = "up" if change > 0 else "down" if change < 0 else "flat"
                self._prices[instrument_key] = {
                    "instrument_key": instrument_key,
                    "ltp": ltp,
                    "prev_close": cp,
                    "change": round(change, 2),
                    "change_pct": round(change_pct, 3),
                    "trend": trend,
                    "volume": _extract_volume(feed),
                    "last_trade_time": ltpc.get("ltt"),
                    "last_trade_qty": ltpc.get("ltq"),
                    "last_tick_monotonic": self._last_message_time,
                    "tick_time": self._last_message_wall.isoformat(),
                }
                updated.append(instrument_key)

        if updated:
            logger.debug("WS tick batch: %d instruments updated", len(updated))
        if self._on_price_update and updated:
            try:
                self._on_price_update(self.get_latest_prices())
            except Exception as e:
                logger.warning("on_price_update callback error: %s", e)

    def _on_error(self, _ws: Any, error: Any) -> None:
        self._last_error = str(error)
        logger.warning("Upstox v3 WebSocket ERROR: %s", error)
        if "401" in str(error) or "Unauthorized" in str(error):
            self.connection_status = "auth_failed"
            self.is_connected = False
            logger.error(
                "WebSocket auth failed — access token is invalid/expired. "
                "Generate a new token in Settings."
            )

    def _on_close(self, _ws: Any, code: Any, msg: Any) -> None:
        self.is_connected = False
        if self._should_run:
            self.connection_status = "reconnecting"
        else:
            self.connection_status = "disconnected"
        logger.info("Upstox v3 WebSocket closed — code=%s msg=%s", code, msg)

    def _on_reconnecting(self, message: Any) -> None:
        self._reconnect_attempts += 1
        self.connection_status = "reconnecting"
        self.is_connected = False
        logger.warning("WebSocket reconnecting: %s", message)

    def _on_reconnect_stopped(self, message: Any) -> None:
        self.connection_status = "disconnected"
        logger.error("WebSocket auto-reconnect stopped: %s", message)
