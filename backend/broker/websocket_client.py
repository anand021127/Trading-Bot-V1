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
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

V3_FEED_URL = "wss://api.upstox.com/v3/feed/market-data-feed"
IST = ZoneInfo("Asia/Kolkata")


def is_nse_market_open() -> bool:
    """Check if NSE/BSE market session is currently active (09:15 to 15:30 IST on weekdays)."""
    now_ist = datetime.now(IST)
    if now_ist.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    market_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now_ist <= market_close


def _extract_ltpc(feed: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the LTPC (last-traded-price-close) block out of a decoded v3 feed
    entry, regardless of which sub-message it arrived in (ltpc / fullFeed.
    marketFF / fullFeed.indexFF / firstLevelWithGreeks).
    """
    if not isinstance(feed, dict):
        try:
            from google.protobuf.json_format import MessageToDict
            feed = MessageToDict(feed)
        except Exception:
            if hasattr(feed, "__dict__"):
                feed = feed.__dict__
            else:
                return {}

    if "ltpc" in feed and isinstance(feed.get("ltpc"), dict):
        return feed.get("ltpc") or {}
    if "ltp" in feed:
        return feed
    full = feed.get("fullFeed") or feed.get("ff") or {}
    if isinstance(full, dict):
        market_ff = full.get("marketFF") or full.get("market_ff")
        index_ff = full.get("indexFF") or full.get("index_ff")
        if isinstance(market_ff, dict):
            if "ltpc" in market_ff and isinstance(market_ff.get("ltpc"), dict):
                return market_ff.get("ltpc") or {}
            if "ltp" in market_ff:
                return market_ff
            e_feed = market_ff.get("eFeed") or market_ff.get("e_feed")
            if isinstance(e_feed, dict) and "ltpc" in e_feed:
                return e_feed.get("ltpc") or {}
        if isinstance(index_ff, dict):
            if "ltpc" in index_ff and isinstance(index_ff.get("ltpc"), dict):
                return index_ff.get("ltpc") or {}
            if "ltp" in index_ff:
                return index_ff
    flwg = feed.get("firstLevelWithGreeks") or feed.get("first_level_with_greeks") or {}
    if isinstance(flwg, dict):
        if "ltpc" in flwg and isinstance(flwg.get("ltpc"), dict):
            return flwg.get("ltpc") or {}
        if "ltp" in flwg:
            return flwg
    opt_greeks = (feed.get("optionGreeks") or feed.get("option_greeks") or
                  (full.get("optionGreeks") if isinstance(full, dict) else None) or
                  (full.get("option_greeks") if isinstance(full, dict) else None) or {})
    if isinstance(opt_greeks, dict):
        if "ltpc" in opt_greeks and isinstance(opt_greeks.get("ltpc"), dict):
            return opt_greeks.get("ltpc") or {}
        if "ltp" in opt_greeks:
            return opt_greeks
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
        self._explicit_token: Optional[str] = access_token.strip().strip('"\'').strip() if (access_token and access_token.strip()) else None
        if self._explicit_token is not None:
            self.access_token = self._explicit_token
        else:
            from backend.broker.token_resolver import resolve_upstox_token
            self.access_token = resolve_upstox_token()
        self._instrument_keys: List[str] = list(instrument_keys or [])
        self._on_price_update = on_price_update
        self.mode = mode

        self._prices: Dict[str, Any] = {}
        self._prices_lock = threading.Lock()

        self.connection_status = "disconnected"
        self.is_connected = False
        self._auth_failed: bool = False
        self._last_message_time: float = 0.0
        self._last_tick_time: float = 0.0
        self._last_error: Optional[str] = None
        self._reconnect_attempts = 0
        self._total_messages: int = 0
        self._ticks_received: int = 0
        self._ignored_messages: int = 0
        self._parse_errors: int = 0
        self._last_reconnect_time: float = 0.0
        self._max_reconnect_attempts: int = 15
        self._base_backoff: float = 2.0
        self._max_backoff: float = 60.0
        self._backoff_delay: float = 2.0

        self._streamer: Any = None
        self._should_run = False

    # ── public API ──────────────────────────────────────────────────────

    def subscribe(self, instrument_keys: List[str]) -> None:
        new_keys = [k for k in instrument_keys if k not in self._instrument_keys]
        self._instrument_keys.extend(new_keys)
        if self._streamer is not None and self.is_connected and new_keys:
            try:
                self._streamer.subscribe(new_keys, self.mode)
                logger.info("WS subscribed to %d new instrument keys: %s", len(new_keys), new_keys[:5])
            except Exception as e:
                logger.warning("WS subscribe failed: %s", e)

    def unsubscribe(self, instrument_keys: List[str]) -> None:
        """Remove instruments from the subscription list. Called when an
        option position is closed and no other position needs that contract."""
        removed = [k for k in instrument_keys if k in self._instrument_keys]
        self._instrument_keys = [k for k in self._instrument_keys if k not in instrument_keys]
        if self._streamer is not None and self.is_connected and removed:
            try:
                self._streamer.unsubscribe(removed)
                logger.info("WS unsubscribed from %d instrument keys: %s", len(removed), removed[:5])
            except Exception as e:
                logger.warning("WS unsubscribe failed: %s", e)

    def get_latest_prices(self) -> Dict[str, Any]:
        with self._prices_lock:
            return dict(self._prices)

    def get_price(self, instrument_key: str) -> Optional[Dict[str, Any]]:
        with self._prices_lock:
            return self._prices.get(instrument_key)

    def is_data_stale(self, max_age_seconds: float = 30.0) -> bool:
        """Check if market feed has not received a valid price tick within max_age_seconds."""
        if self._last_tick_time == 0:
            return True
        return (time.monotonic() - self._last_tick_time) > max_age_seconds

    def get_tick_age(self, instrument_key: str) -> Optional[float]:
        """Return the age of the last tick for a specific instrument in seconds.
        Returns None if no tick has ever been received for this key."""
        with self._prices_lock:
            entry = self._prices.get(instrument_key)
        if not entry:
            return None
        tick_mono = entry.get("last_tick_monotonic")
        if not tick_mono:
            return None
        return round(time.monotonic() - tick_mono, 1)

    @property
    def market_data_status(self) -> str:
        """Separate from connection_status — the WebSocket can be CONNECTED
        but NOT RECEIVING DATA (stale / market closed). This property distinguishes:
          LIVE           — connected and recent ticks received
          STALE          — connected during market hours but no recent ticks received
          MARKET_CLOSED  — connected outside regular market hours (feed legitimately idle)
          UNAVAILABLE    — not connected at all
        """
        if not self.is_connected:
            return "UNAVAILABLE"
        if is_nse_market_open():
            if self._last_tick_time > 0 and (time.monotonic() - self._last_tick_time) <= 30.0:
                return "LIVE"
            return "STALE"
        else:
            if self._last_tick_time > 0 and (time.monotonic() - self._last_tick_time) <= 30.0:
                return "LIVE"
            return "MARKET_CLOSED"

    def status_report(self) -> Dict[str, Any]:
        """Everything the diagnostics/dashboard UI needs to show honestly."""
        now = time.monotonic()
        market_open = is_nse_market_open()
        tick_age = (
            round(now - self._last_tick_time, 1)
            if self._last_tick_time else None
        )
        msg_age = (
            round(now - self._last_message_time, 1)
            if self._last_message_time else None
        )
        from backend.broker.token_resolver import token_fingerprint
        fp = token_fingerprint(self.access_token) if self.access_token else None

        auth_status = "NO_TOKEN"
        if self._auth_failed:
            auth_status = "AUTH_FAILED_401"
        elif self.is_connected:
            auth_status = "AUTHENTICATED"
        elif self.access_token:
            auth_status = "CONNECTING" if self.connection_status == "connecting" else "TOKEN_PRESENT"

        return {
            "connection_status": self.connection_status,
            "is_connected": self.is_connected,
            "auth_failed": self._auth_failed,
            "auth_status": auth_status,
            "token_fingerprint": fp,
            "market_open": market_open,
            "market_data_status": self.market_data_status,
            "subscribed_instruments": len(self._instrument_keys),
            "instrument_keys": list(self._instrument_keys),
            "last_tick_age_seconds": tick_age,
            "last_message_age_seconds": msg_age,
            "is_stale": self.is_data_stale(),
            "last_error": self._last_error,
            "reconnect_attempts": self._reconnect_attempts,
            "total_messages_received": self._total_messages,
            "ticks_received": self._ticks_received,
            "ignored_messages_count": self._ignored_messages,
            "parse_errors_count": self._parse_errors,
            "last_reconnect_seconds_ago": (
                round(now - self._last_reconnect_time, 1)
                if self._last_reconnect_time else None
            ),
            "feed_endpoint": V3_FEED_URL,
        }

    def start(self) -> None:
        if self._explicit_token:
            self.access_token = self._explicit_token
        else:
            from backend.broker.token_resolver import resolve_upstox_token
            self.access_token = resolve_upstox_token()

        if not self.access_token:
            self.connection_status = "auth_failed"
            self._auth_failed = True
            self.is_connected = False
            self._last_error = "No Upstox access token configured"
            logger.warning("WebSocket not started — no access token")
            return

        from backend.broker.token_resolver import check_token_freshness, token_fingerprint
        freshness = check_token_freshness(self.access_token)
        if freshness.get("is_expired") is True:
            self.connection_status = "auth_failed"
            self._auth_failed = True
            self.is_connected = False
            self._last_error = f"Cannot start WebSocket: access token is expired ({freshness.get('message')})"
            logger.error(
                "WebSocket start aborted — access token is expired (fingerprint=%s). Refresh via OAuth.",
                freshness.get("token_fingerprint"),
            )
            return

        if self._should_run:
            logger.debug("WebSocket already running")
            return

        try:
            import upstox_client  # noqa: F401
        except ImportError:
            self.connection_status = "disconnected"
            self.is_connected = False
            self._last_error = "upstox-python-sdk not installed"
            logger.error(
                "upstox-python-sdk is not installed. "
                "Run: pip install upstox-python-sdk websocket-client"
            )
            return

        self._auth_failed = False
        self._should_run = True
        self.connection_status = "connecting"
        self.is_connected = False
        fp = token_fingerprint(self.access_token)
        logger.info(
            "Starting Upstox v3 WebSocket client — %d instruments, mode=%s, token_fingerprint=%s",
            len(self._instrument_keys), self.mode, fp,
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

    def reconnect_with_token(self, new_token: Optional[str] = None) -> None:
        """Reconnect WebSocket with canonical access token without server restart.
        
        Resets auth failure state, cancels existing streamer, updates the token,
        and reconnects cleanly.
        """
        if new_token and new_token.strip():
            clean = new_token.strip().strip('"\'').strip()
            self._explicit_token = clean
            self.access_token = clean
        else:
            self._explicit_token = None
            from backend.broker.token_resolver import resolve_upstox_token
            self.access_token = resolve_upstox_token()

        if not self.access_token:
            self.connection_status = "auth_failed"
            self._auth_failed = True
            self.is_connected = False
            self._last_error = "No Upstox access token available for reconnect"
            logger.warning("WebSocket reconnect aborted — no access token")
            return

        from backend.broker.token_resolver import check_token_freshness, token_fingerprint
        freshness = check_token_freshness(self.access_token)
        if freshness.get("is_expired") is True:
            self.connection_status = "auth_failed"
            self._auth_failed = True
            self.is_connected = False
            self._last_error = f"Cannot reconnect WebSocket: token is expired ({freshness.get('message')})"
            logger.error(
                "WebSocket reconnect aborted — token is expired (fingerprint=%s)",
                freshness.get("token_fingerprint"),
            )
            return

        fp = token_fingerprint(self.access_token)
        logger.info("WebSocket reconnecting with fresh token (fingerprint=%s)", fp)

        self._auth_failed = False
        self._reconnect_attempts = 0
        self._backoff_delay = self._base_backoff
        self._last_error = None

        if self._streamer is not None:
            try:
                self._streamer.auto_reconnect(False)
                self._streamer.disconnect()
            except Exception:
                pass
            self._streamer = None

        self._should_run = True
        self.connection_status = "connecting"
        self.is_connected = False
        self._build_and_connect()

    # ── internals ────────────────────────────────────────────────────────

    def _build_and_connect(self) -> None:
        import upstox_client

        configuration = upstox_client.Configuration()
        configuration.access_token = self.access_token
        api_client = upstox_client.ApiClient(configuration)

        streamer = upstox_client.MarketDataStreamerV3(
            api_client, self._instrument_keys, self.mode,
        )
        retry_interval = int(max(1.0, self._backoff_delay))
        streamer.auto_reconnect(True, interval=retry_interval, retry_count=self._max_reconnect_attempts)

        streamer.on("open", self._on_open)
        streamer.on("message", self._on_message)
        streamer.on("error", self._on_error)
        streamer.on("close", self._on_close)
        streamer.on("reconnecting", self._on_reconnecting)
        streamer.on("autoReconnectStopped", self._on_reconnect_stopped)

        self._streamer = streamer
        streamer.connect()  # non-blocking — SDK runs the socket in a thread

    def _on_open(self, *args: Any, **kwargs: Any) -> None:
        self.is_connected = True
        self.connection_status = "connected"
        self._reconnect_attempts = 0
        self._last_error = None
        logger.info(
            "Upstox v3 WebSocket CONNECTED (%s) — %d instruments subscribed",
            V3_FEED_URL, len(self._instrument_keys),
        )
        # Lifecycle event
        try:
            from backend.health.health_monitor import health_monitor, ComponentStatus
            health_monitor.update_status("websocket", ComponentStatus.RUNNING)
            health_monitor.log_event(
                "websocket", "WS_CONNECTED",
                f"Upstox v3 WebSocket connected — {len(self._instrument_keys)} instruments",
            )
        except Exception:
            pass
        # Re-subscribe to all instruments on reconnect (including dynamically
        # added option contracts that may have been subscribed after initial
        # connection). The SDK's auto-reconnect restores the initial set but
        # NOT keys added via subscribe() after the first connect.
        if self._instrument_keys and self._streamer is not None:
            try:
                self._streamer.subscribe(self._instrument_keys, self.mode)
                logger.info("Re-subscribed %d instruments after connect/reconnect", len(self._instrument_keys))
            except Exception as e:
                logger.warning("Re-subscribe on connect failed: %s", e)

    def _on_message(self, *args: Any, **kwargs: Any) -> None:
        """`data` is either protobuf bytes, JSON string, or decoded FeedResponse dict:
        {"type": "...", "feeds": {instrument_key: {...}}, "currentTs": "..."}
        Handles both 1-arg `_on_message(data)` and 2-arg `_on_message(ws, data)`
        callback signatures seamlessly.
        """
        data: Any = None
        if args:
            if len(args) == 1:
                data = args[0]
            else:
                data = args[1]
        elif "data" in kwargs:
            data = kwargs["data"]
        elif "message" in kwargs:
            data = kwargs["message"]

        if data is None:
            return

        self._last_message_time = time.monotonic()
        self._total_messages += 1

        # If data is bytes (raw protobuf), decode using FeedResponse
        if isinstance(data, bytes):
            try:
                from upstox_client.feeder.MarketDataFeedV3_pb2 import FeedResponse
                from google.protobuf.json_format import MessageToDict
                feed_response = FeedResponse()
                feed_response.ParseFromString(data)
                data = MessageToDict(feed_response)
            except Exception as e:
                self._parse_errors += 1
                logger.debug("Failed to decode protobuf bytes: %s", e)
                return
        elif isinstance(data, str):
            try:
                import json
                data = json.loads(data)
            except Exception:
                self._parse_errors += 1
                return
        elif not isinstance(data, dict):
            try:
                from google.protobuf.json_format import MessageToDict
                data = MessageToDict(data)
            except Exception:
                if hasattr(data, "__dict__"):
                    data = data.__dict__
                else:
                    self._parse_errors += 1
                    return

        msg_type = data.get("type") if isinstance(data, dict) else None
        if msg_type == "market_info":
            self._ignored_messages += 1
            logger.debug("WS market_info tick: %s", data.get("marketInfo"))
            return

        feeds = data.get("feeds") if isinstance(data, dict) else None
        if not isinstance(feeds, dict):
            feeds = data if isinstance(data, dict) else {}

        if not feeds:
            self._ignored_messages += 1
            return

        updated: List[str] = []
        with self._prices_lock:
            for instrument_key, feed in feeds.items():
                if not isinstance(feed, dict):
                    continue
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
                self._prices[instrument_key] = {
                    "instrument_key": instrument_key,
                    "ltp": ltp,
                    "prev_close": cp,
                    "change": round(change, 2),
                    "change_pct": round(change_pct, 3),
                    "volume": _extract_volume(feed),
                    "last_trade_time": ltpc.get("ltt"),
                    "last_trade_qty": ltpc.get("ltq"),
                    "last_tick_monotonic": self._last_message_time,
                }
                updated.append(instrument_key)

        if updated:
            self._ticks_received += len(updated)
            self._last_tick_time = time.monotonic()
            logger.debug("WS tick batch: %d instruments updated", len(updated))
        else:
            self._ignored_messages += 1
        if self._on_price_update and updated:
            try:
                self._on_price_update(self.get_latest_prices())
            except Exception as e:
                logger.warning("on_price_update callback error: %s", e)

    def _on_error(self, *args: Any, **kwargs: Any) -> None:
        error = args[1] if len(args) >= 2 else (args[0] if args else kwargs.get("error", "Unknown error"))
        err_str = str(error)
        self._last_error = err_str
        logger.warning("Upstox v3 WebSocket ERROR: %s", err_str)
        try:
            from backend.health.health_monitor import health_monitor
            health_monitor.record_error("websocket", err_str)
        except Exception:
            pass

        # CRITICAL: Halt auto-reconnects on 401 Unauthorized / invalid token
        if "401" in err_str or "Unauthorized" in err_str or "UDAPI100050" in err_str:
            self.connection_status = "auth_failed"
            self._auth_failed = True
            self.is_connected = False
            self._should_run = False
            if self._streamer is not None:
                try:
                    self._streamer.auto_reconnect(False)
                    self._streamer.disconnect()
                except Exception:
                    pass
            logger.error(
                "WebSocket auth failed (HTTP 401 Unauthorized) — stopped automatic reconnects. "
                "Fresh OAuth token required via Settings."
            )
            try:
                from backend.health.health_monitor import health_monitor, ComponentStatus
                health_monitor.update_status("websocket", ComponentStatus.FAILED)
                health_monitor.log_event(
                    "websocket", "WS_AUTH_FAILED_401",
                    "WebSocket auth failed (401 Unauthorized). Stopped reconnecting until token refresh.",
                    severity="ERROR"
                )
            except Exception:
                pass

    def _on_close(self, *args: Any, **kwargs: Any) -> None:
        code = None
        msg = None
        if len(args) >= 3:
            code = args[1]
            msg = args[2]
        elif len(args) == 2:
            code = args[0]
            msg = args[1]
        elif len(args) == 1:
            msg = args[0]

        self.is_connected = False
        if self._auth_failed:
            self.connection_status = "auth_failed"
            logger.info("Upstox v3 WebSocket closed after auth failure (will not reconnect without new token)")
            return

        if self._should_run:
            self.connection_status = "reconnecting"
            # Exponential bounded backoff calculation for next attempt
            self._backoff_delay = min(self._max_backoff, self._base_backoff * (1.5 ** min(self._reconnect_attempts, 8)))
        else:
            self.connection_status = "disconnected"
        logger.info("Upstox v3 WebSocket closed — code=%s msg=%s", code, msg)
        try:
            from backend.health.health_monitor import health_monitor, ComponentStatus
            status = ComponentStatus.RECONNECTING if self._should_run else ComponentStatus.STOPPED
            health_monitor.update_status("websocket", status)
            health_monitor.log_event(
                "websocket", "WS_DISCONNECTED",
                f"WebSocket closed — code={code} msg={msg}",
                severity="WARNING" if self._should_run else "INFO",
            )
        except Exception:
            pass

    def _on_reconnecting(self, *args: Any, **kwargs: Any) -> None:
        if self._auth_failed or not self._should_run:
            if self._streamer is not None:
                try:
                    self._streamer.auto_reconnect(False)
                    self._streamer.disconnect()
                except Exception:
                    pass
            self.connection_status = "auth_failed" if self._auth_failed else "disconnected"
            return

        message = args[-1] if args else kwargs.get("message", "reconnecting")
        self._reconnect_attempts += 1
        self._last_reconnect_time = time.monotonic()
        self.connection_status = "reconnecting"
        self.is_connected = False
        logger.warning(
            "WebSocket reconnecting (attempt %d/%d, backoff=%.1fs): %s",
            self._reconnect_attempts, self._max_reconnect_attempts, self._backoff_delay, message
        )
        try:
            from backend.health.health_monitor import health_monitor, ComponentStatus
            health_monitor.update_status("websocket", ComponentStatus.RECONNECTING)
            health_monitor.log_event(
                "websocket", "WS_RECONNECTING",
                f"WebSocket reconnecting (attempt {self._reconnect_attempts}): {message}",
                severity="WARNING",
            )
        except Exception:
            pass

    def _on_reconnect_stopped(self, *args: Any, **kwargs: Any) -> None:
        message = args[-1] if args else kwargs.get("message", "auto-reconnect stopped")
        self.connection_status = "disconnected"
        self.is_connected = False
        logger.error("WebSocket auto-reconnect stopped: %s", message)
