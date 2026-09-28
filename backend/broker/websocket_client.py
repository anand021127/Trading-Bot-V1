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

LIFECYCLE — explicit state machine
----------------------------------
    DISCONNECTED → CONNECTING → CONNECTED → SUBSCRIBED → STREAMING
                        ↑                                  │
                        └──── RECONNECT_WAIT ←─────────────┘
    any state → FAILED (auth failure / build failure, terminal until restart)

Rules enforced by this module (the "stuck RECONNECTING / 'NoneType' object
has no attribute 'sock'" class of bug is impossible by construction):

 1. SINGLE OWNER — exactly one socket generation is authoritative at any
    moment. Every start()/reconnect_with_token()/stop()/auth-401 teardown
    bumps an integer *generation* under one lock. SDK event handlers are
    generation-bound at registration: a late callback from a retired
    generation (e.g. the old run_forever thread that the SDK cannot join)
    is dropped BEFORE it can touch state, subscriptions, or status.
 2. NO SOCKET ACCESS WHEN NOT OPEN — the SDK owns the raw socket. We only
    ever call streamer.subscribe/unsubscribe when the current generation is
    actually in CONNECTED/SUBSCRIBED/STREAMING, and every such call is
    exception-guarded so a socket dying mid-call can never surface as an
    error storm ("WebSocket is not open.").
 3. NO DUPLICATE RECONNECT MACHINERY — the SDK's built-in auto-reconnect
    (bounded: interval + retry_count) is the ONLY reconnect mechanism for a
    live generation. We never layer a second competing loop on top; our
    reconnect_with_token() is a deliberate generation replacement used by
    the OAuth token-refresh flow.
 4. STREAMING IS EARNED — STREAMING is reachable ONLY by decoding a real
    market-data message. Connection/handshake/subscription success can
    never fake it, so the API diagnostic can honestly distinguish
    connected-but-not-streaming (e.g. market closed) from real streaming.

No mock/synthetic prices are ever produced here. If the token is missing,
invalid, or the feed is down, `get_latest_prices()` simply stays empty and
`status_report()` reports the real state — callers must render that
honestly rather than fabricate numbers.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

V3_FEED_URL = "wss://api.upstox.com/v3/feed/market-data-feed"
IST = ZoneInfo("Asia/Kolkata")

# ── Explicit lifecycle states ────────────────────────────────────────────
STATE_DISCONNECTED = "disconnected"    # no socket; deliberate stop or never started
STATE_CONNECTING = "connecting"        # connect() issued, handshake not confirmed
STATE_CONNECTED = "connected"          # on_open received (handshake complete)
STATE_SUBSCRIBED = "subscribed"        # subscription payload accepted after open
STATE_STREAMING = "streaming"          # ≥1 real market-data message decoded
STATE_RECONNECT_WAIT = "reconnect_wait"  # down; SDK bounded auto-reconnect active
STATE_FAILED = "failed"                # terminal: auth failure / build failure

# States an `open` event may legitimately transition from. (FAILED is
# deliberately excluded — a generation that failed auth never comes back.)
_STATE_OPEN_ALLOWED = frozenset({
    STATE_DISCONNECTED, STATE_CONNECTING, STATE_RECONNECT_WAIT,
    STATE_CONNECTED, STATE_SUBSCRIBED, STATE_STREAMING,
})

# Only a decoded market-data message may earn STREAMING, and only while the
# socket is actually open.
_STATE_STREAMING_ALLOWED = frozenset({
    STATE_CONNECTED, STATE_SUBSCRIBED, STATE_STREAMING,
})


def is_nse_market_open() -> bool:
    """Whether the NSE/BSE equity+F&O session is currently active.

    Delegates to the ONE authoritative exchange calendar (weekends, official
    NSE/BSE holidays, and special sessions like Muhurat) — never local
    weekday math. Falls back to the classic weekday/session check only if the
    calendar itself cannot be loaded, so a broken feed connection probe can
    never take the whole client down.
    """
    try:
        from backend.market.calendar import is_market_open_now
        return is_market_open_now()
    except Exception:  # pragma: no cover — calendar import must never crash the feed
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
                                               'auth_failed' (legacy vocabulary,
                                               derived from the state machine)
      - `state`                             — explicit lifecycle state
      - `is_data_stale(max_age_seconds)`    — staleness check for the UI

    Thread-safety: the SDK fires events on ITS OWN socket threads. Every
    state transition goes through `self._state_lock`; every event handler is
    generation-bound so a retired socket thread can never mutate our state.
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

        # ── lifecycle state machine (guarded by _state_lock) ─────────────
        self._state_lock = threading.Lock()
        self._state: str = STATE_DISCONNECTED
        self._generation: int = 0        # bumps on every (re)build/teardown
        self._streamer: Any = None       # CURRENT generation's SDK streamer only
        self._should_run = False

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

    # ── state machine internals ──────────────────────────────────────────

    def _get_state(self) -> str:
        with self._state_lock:
            return self._state

    def _set_state(self, new_state: str, only_if: Optional[frozenset] = None) -> bool:
        """Transition the lifecycle state. When `only_if` is given the
        transition happens only from one of those states (invalid transitions
        are refused and logged). Returns True when the transition happened."""
        with self._state_lock:
            return self._set_state_locked(new_state, only_if)

    def _set_state_locked(self, new_state: str, only_if: Optional[frozenset] = None) -> bool:
        """_set_state variant for callers already holding _state_lock."""
        if only_if is not None and self._state not in only_if:
            logger.debug(
                "WS state transition %s → %s refused (invalid from %s)",
                self._state, new_state, self._state,
            )
            return False
        if self._state != new_state:
            logger.debug("WS state: %s → %s", self._state, new_state)
            self._state = new_state
        return True

    def _current_generation(self) -> int:
        with self._state_lock:
            return self._generation

    def _is_current(self, generation: Optional[int]) -> bool:
        """Generation fence: every SDK event handler calls this FIRST. A
        callback from a retired generation (orphaned run_forever thread that
        the SDK cannot join) is dropped before it can touch anything."""
        if generation is None:
            return True  # direct/unbound invocation (tests, legacy callers)
        with self._state_lock:
            return generation == self._generation

    def _current_streamer(self) -> Any:
        with self._state_lock:
            return self._streamer

    def _teardown_streamer(self, streamer: Any) -> None:
        """Tear down a streamer WITHOUT holding the lock. Safe to call with
        None, any number of times, on an already-dead streamer. Every failure
        is swallowed — cleanup must never be the thing that kills the feed.

        Note: streamer.auto_reconnect(False) makes the SDK emit
        autoReconnectStopped("Disabled by client."). That event is
        generation-fenced, so for a retired generation it is dropped here
        and no longer logged as an alarming ERROR.
        """
        if streamer is None:
            return
        try:
            streamer.auto_reconnect(False)
        except Exception:
            pass
        try:
            streamer.disconnect()
        except Exception:
            pass

    def _retire_current_streamer_locked(self) -> Any:
        """Swap out the current streamer under the lock; returns the retired
        object (caller tears it down WITHOUT the lock). Bumps the generation
        so every in-flight callback of the retired streamer becomes stale."""
        retired = self._streamer
        self._streamer = None
        self._generation += 1
        return retired

    # ── public API ──────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        """Explicit lifecycle state — see STATE_* constants."""
        return self._get_state()

    @property
    def is_connected(self) -> bool:
        """True only when the socket is genuinely open (CONNECTED or beyond).
        Derived from the state machine — no separate flag to drift out of
        sync."""
        return self._get_state() in (STATE_CONNECTED, STATE_SUBSCRIBED, STATE_STREAMING)

    @property
    def connection_status(self) -> str:
        """Legacy status vocabulary (unchanged external contract), now DERIVED
        from the explicit state machine:
          connected    ← CONNECTED / SUBSCRIBED / STREAMING
          reconnecting ← RECONNECT_WAIT
          auth_failed  ← FAILED with an auth failure flag
        Consumers reading the richer truth should use `status_report()['state']`.
        """
        state = self._get_state()
        if state == STATE_CONNECTING:
            return "connecting"
        if state in (STATE_CONNECTED, STATE_SUBSCRIBED, STATE_STREAMING):
            return "connected"
        if state == STATE_RECONNECT_WAIT:
            return "reconnecting"
        if state == STATE_FAILED:
            return "auth_failed" if self._auth_failed else "disconnected"
        return "disconnected"

    def subscribe(self, instrument_keys: List[str]) -> None:
        new_keys = [k for k in instrument_keys if k not in self._instrument_keys]
        if not new_keys:
            return
        self._instrument_keys.extend(new_keys)
        self._send_subscription(new_keys, add=True)

    def unsubscribe(self, instrument_keys: List[str]) -> None:
        """Remove instruments from the subscription list. Called when an
        option position is closed and no other position needs that contract."""
        removed = [k for k in instrument_keys if k in self._instrument_keys]
        if not removed:
            return
        self._instrument_keys = [k for k in self._instrument_keys if k not in instrument_keys]
        self._send_subscription(removed, add=False)

    def _send_subscription(self, keys: List[str], *, add: bool) -> None:
        """Send a subscribe/unsubscribe frame — only if the CURRENT generation
        actually has an open socket. Never raises: a socket being torn down
        concurrently must not resurrect the 'WebSocket is not open.' error
        storm, and a None/closed socket is never touched."""
        with self._state_lock:
            streamer = self._streamer
            open_state = self._state in (STATE_CONNECTED, STATE_SUBSCRIBED, STATE_STREAMING)
        if streamer is None or not open_state:
            logger.debug("WS %s for %d keys deferred — socket not open", "subscribe" if add else "unsubscribe", len(keys))
            return
        try:
            if add:
                streamer.subscribe(keys, self.mode)
            else:
                streamer.unsubscribe(keys)
            logger.info(
                "WS %s %d instrument keys: %s",
                "subscribed to" if add else "unsubscribed from",
                len(keys), keys[:5],
            )
        except Exception as e:
            logger.warning("WS %s failed for %d keys: %s", "subscribe" if add else "unsubscribe", len(keys), e)

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
        recent = self._last_tick_time > 0 and (time.monotonic() - self._last_tick_time) <= 30.0
        if recent:
            return "LIVE"
        return "STALE" if is_nse_market_open() else "MARKET_CLOSED"

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
            "state": self._get_state(),
            "streaming": self._get_state() == STATE_STREAMING,
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
            self._auth_failed = True
            self._set_state(STATE_FAILED)
            self._last_error = "No Upstox access token configured"
            logger.warning("WebSocket not started — no access token")
            return

        from backend.broker.token_resolver import check_token_freshness, token_fingerprint
        freshness = check_token_freshness(self.access_token)
        if freshness.get("is_expired") is True:
            self._auth_failed = True
            self._set_state(STATE_FAILED)
            self._last_error = f"Cannot start WebSocket: access token is expired ({freshness.get('message')})"
            logger.error(
                "WebSocket start aborted — access token is expired (fingerprint=%s). Refresh via OAuth.",
                freshness.get("token_fingerprint"),
            )
            return

        with self._state_lock:
            if self._should_run:
                already_running = True
            else:
                already_running = False
                self._should_run = True
                self._generation += 1  # new authoritative generation
        if already_running:
            logger.debug("WebSocket already running (state=%s)", self._get_state())
            return

        try:
            import upstox_client  # noqa: F401
        except ImportError:
            with self._state_lock:
                self._should_run = False
                self._set_state_locked(STATE_FAILED)
            self._last_error = "upstox-python-sdk not installed"
            logger.error(
                "upstox-python-sdk is not installed. "
                "Run: pip install upstox-python-sdk websocket-client"
            )
            return

        self._auth_failed = False
        self._last_error = None
        self._reconnect_attempts = 0
        self._backoff_delay = self._base_backoff
        self._set_state(STATE_CONNECTING)
        fp = token_fingerprint(self.access_token)
        logger.info(
            "Starting Upstox v3 WebSocket client — %d instruments, mode=%s, token_fingerprint=%s, generation=%d",
            len(self._instrument_keys), self.mode, fp, self._generation,
        )
        self._build_and_connect()

    def stop(self) -> None:
        """Deliberate stop — idempotent, and safe to call from any thread.
        Retires the current generation FIRST (so late SDK callbacks are
        fenced off), then tears the streamer down without the lock."""
        with self._state_lock:
            self._should_run = False
            retired = self._retire_current_streamer_locked()
        self._teardown_streamer(retired)
        self._set_state(STATE_DISCONNECTED)
        logger.info("WebSocket disconnected cleanly")

    def reconnect_with_token(self, new_token: Optional[str] = None) -> None:
        """Reconnect WebSocket with canonical access token without server restart.

        This is the ONE token-refresh path (used by the OAuth callback): it
        retires the old generation cleanly (old socket invalidated, its late
        callbacks fenced), applies the new token, and builds exactly ONE new
        generation. Never logs the token itself — fingerprints only.
        """
        if new_token and new_token.strip():
            clean = new_token.strip().strip('"\'').strip()
        else:
            clean = None

        # Idempotence guard: the OAuth callback path triggers BOTH
        # invalidate_old_token_references() and _restart_websocket_client(),
        # each of which calls this method. A refresh carrying the token this
        # client ALREADY runs on, while a generation is still healthy, must
        # not tear down and rebuild again — one token refresh, one rebuild,
        # one owner. A dead/failed generation is always rebuilt.
        if (
            clean is not None
            and clean == self.access_token
            and self._get_state() in (
                STATE_CONNECTING, STATE_CONNECTED, STATE_SUBSCRIBED, STATE_STREAMING,
            )
        ):
            from backend.broker.token_resolver import token_fingerprint
            logger.info(
                "WS reconnect skipped — token %s already active and generation "
                "healthy (state=%s)", token_fingerprint(clean), self._get_state(),
            )
            return

        if clean is not None:
            self._explicit_token = clean
            self.access_token = clean
        else:
            self._explicit_token = None
            from backend.broker.token_resolver import resolve_upstox_token
            self.access_token = resolve_upstox_token()

        if not self.access_token:
            self._auth_failed = True
            self._set_state(STATE_FAILED)
            self._last_error = "No Upstox access token available for reconnect"
            logger.warning("WebSocket reconnect aborted — no access token")
            return

        from backend.broker.token_resolver import check_token_freshness, token_fingerprint
        freshness = check_token_freshness(self.access_token)
        if freshness.get("is_expired") is True:
            self._auth_failed = True
            self._set_state(STATE_FAILED)
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

        # Retire the old generation under the lock; tear the old streamer
        # down WITHOUT the lock. Any callback the dying old socket thread
        # still fires is generation-stale and gets dropped.
        with self._state_lock:
            retired = self._retire_current_streamer_locked()
            self._should_run = True
        self._teardown_streamer(retired)

        self._set_state(STATE_CONNECTING)
        self._build_and_connect()

    # ── internals ────────────────────────────────────────────────────────

    def _build_and_connect(self) -> None:
        """Create exactly ONE new SDK streamer for the CURRENT generation and
        connect. The SDK's connect() is fire-and-forget (it runs the socket
        in its own thread and cannot be joined), so instead of waiting we
        generation-bind every event handler: a late event from THIS streamer
        after a newer generation exists is dropped before it can do harm.
        The SDK's own bounded auto-reconnect is the only reconnect mechanism
        while this generation lives."""
        try:
            import upstox_client
        except ImportError:
            with self._state_lock:
                self._should_run = False
                self._set_state_locked(STATE_FAILED)
            self._last_error = "upstox-python-sdk not installed"
            logger.error(
                "upstox-python-sdk is not installed. "
                "Run: pip install upstox-python-sdk websocket-client"
            )
            return

        configuration = upstox_client.Configuration()
        configuration.access_token = self.access_token
        api_client = upstox_client.ApiClient(configuration)

        streamer = upstox_client.MarketDataStreamerV3(
            api_client, self._instrument_keys, self.mode,
        )
        retry_interval = int(max(1.0, self._backoff_delay))
        streamer.auto_reconnect(True, interval=retry_interval, retry_count=self._max_reconnect_attempts)

        gen = self._current_generation()  # bind handlers to THIS generation

        streamer.on("open", lambda *a, **kw: self._on_open(*a, _ws_generation=gen, **kw))
        streamer.on("message", lambda *a, **kw: self._on_message(*a, _ws_generation=gen, **kw))
        streamer.on("error", lambda *a, **kw: self._on_error(*a, _ws_generation=gen, **kw))
        streamer.on("close", lambda *a, **kw: self._on_close(*a, _ws_generation=gen, **kw))
        streamer.on("reconnecting", lambda *a, **kw: self._on_reconnecting(*a, _ws_generation=gen, **kw))
        streamer.on("autoReconnectStopped", lambda *a, **kw: self._on_reconnect_stopped(*a, _ws_generation=gen, **kw))

        with self._state_lock:
            if gen != self._generation:
                # A newer generation took over while we were building —
                # this streamer must never be connected or referenced.
                logger.warning("WS generation %d superseded during connect — aborting build", gen)
                superseded = streamer
            else:
                self._streamer = streamer
                superseded = None
        if superseded is not None:
            self._teardown_streamer(superseded)
            return

        streamer.connect()  # non-blocking — SDK runs the socket in a thread

    # ── SDK event handlers (all generation-fenced) ───────────────────────

    def _on_open(self, *args: Any, _ws_generation: Optional[int] = None, **kwargs: Any) -> None:
        """SDK 'open' event — handshake complete. Subscription is sent ONLY
        here (never before the socket is actually open)."""
        if not self._is_current(_ws_generation):
            logger.debug("Ignoring stale WS open from generation %s", _ws_generation)
            return

        if not self._set_state(STATE_CONNECTED, only_if=_STATE_OPEN_ALLOWED):
            return

        self._reconnect_attempts = 0
        self._backoff_delay = self._base_backoff  # reconnect state fully reset once the socket is back
        self._last_error = None
        logger.info(
            "Upstox v3 WebSocket CONNECTED (%s) — %d instruments pending subscribe",
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

        # Subscribe ONLY now that the socket is open. Re-subscribe the full
        # set (including dynamically added option contracts) so SDK
        # auto-reconnects restore everything, not just the initial keys.
        streamer = self._current_streamer()
        if self._instrument_keys and streamer is not None:
            try:
                streamer.subscribe(list(self._instrument_keys), self.mode)
                self._set_state(
                    STATE_SUBSCRIBED,
                    only_if=frozenset({STATE_CONNECTED, STATE_SUBSCRIBED, STATE_STREAMING}),
                )
                logger.info(
                    "Subscribed %d instruments after connect (generation=%s)",
                    len(self._instrument_keys), _ws_generation,
                )
            except Exception as e:
                # Socket may have died between open and subscribe — the SDK's
                # bounded reconnect will re-open and we re-subscribe there.
                logger.warning("Subscribe on connect failed (will retry on reconnect): %s", e)

    def _on_message(self, *args: Any, _ws_generation: Optional[int] = None, **kwargs: Any) -> None:
        """`data` is either protobuf bytes, JSON string, or decoded FeedResponse dict:
        {"type": "...", "feeds": {instrument_key: {...}}, "currentTs": "..."}
        Handles both 1-arg `_on_message(data)` and 2-arg `_on_message(ws, data)`
        callback signatures seamlessly.

        This is the ONLY place that can earn STATE_STREAMING — a real,
        decoded market-data message. Connection/subscription success can
        never fake it.
        """
        if not self._is_current(_ws_generation):
            logger.debug("Ignoring stale WS message from generation %s", _ws_generation)
            return

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
            # FIRST REAL TICK (and every tick after) keeps/earns STREAMING.
            self._set_state(STATE_STREAMING, only_if=_STATE_STREAMING_ALLOWED)
            logger.debug("WS tick batch: %d instruments updated", len(updated))
        else:
            self._ignored_messages += 1
        if self._on_price_update and updated:
            try:
                self._on_price_update(self.get_latest_prices())
            except Exception as e:
                logger.warning("on_price_update callback error: %s", e)

    def _on_error(self, *args: Any, _ws_generation: Optional[int] = None, **kwargs: Any) -> None:
        """SDK 'error' event. Never touches the socket. A 401 marks the
        generation FAILED, retires it (stopping SDK reconnects), and requires
        a fresh OAuth token."""
        error = args[1] if len(args) >= 2 else (args[0] if args else kwargs.get("error", "Unknown error"))
        err_str = str(error)

        if not self._is_current(_ws_generation):
            logger.debug("Ignoring stale WS error from generation %s: %s", _ws_generation, err_str)
            return

        self._last_error = err_str
        logger.warning("Upstox v3 WebSocket ERROR: %s", err_str)
        try:
            from backend.health.health_monitor import health_monitor
            health_monitor.record_error("websocket", err_str)
        except Exception:
            pass

        # CRITICAL: Halt auto-reconnects on 401 Unauthorized / invalid token
        if "401" in err_str or "Unauthorized" in err_str or "UDAPI100050" in err_str:
            self._auth_failed = True
            self._should_run = False
            self._set_state(STATE_FAILED)
            with self._state_lock:
                retired = self._retire_current_streamer_locked()
            self._teardown_streamer(retired)
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

    def _on_close(self, *args: Any, _ws_generation: Optional[int] = None, **kwargs: Any) -> None:
        """SDK 'close' event — code/msg extraction is unchanged; state goes to
        RECONNECT_WAIT (SDK bounded reconnect active) or DISCONNECTED
        (deliberate stop). Never touches the socket."""
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

        if not self._is_current(_ws_generation):
            logger.debug("Ignoring stale WS close from generation %s (code=%s msg=%s)", _ws_generation, code, msg)
            return

        if self._auth_failed:
            self._set_state(STATE_FAILED)
            logger.info("Upstox v3 WebSocket closed after auth failure (will not reconnect without new token)")
            return

        with self._state_lock:
            if self._should_run:
                # Bounded exponential backoff for the NEXT attempt window.
                self._backoff_delay = min(
                    self._max_backoff,
                    self._base_backoff * (1.5 ** min(self._reconnect_attempts, 8)),
                )
                self._set_state_locked(STATE_RECONNECT_WAIT)
            else:
                self._set_state_locked(STATE_DISCONNECTED)

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

    def _on_reconnecting(self, *args: Any, _ws_generation: Optional[int] = None, **kwargs: Any) -> None:
        """SDK 'reconnecting' event. Pure bookkeeping — this handler performs
        ZERO socket access (the 'NoneType' object has no attribute 'sock'
        class of bug is impossible from here)."""
        if not self._is_current(_ws_generation):
            logger.debug("Ignoring stale WS reconnecting from generation %s", _ws_generation)
            return

        if self._auth_failed or not self._should_run:
            with self._state_lock:
                retired = self._retire_current_streamer_locked()
                stop_auth = self._auth_failed
            self._teardown_streamer(retired)
            self._set_state(STATE_FAILED if stop_auth else STATE_DISCONNECTED)
            return

        message = args[-1] if args else kwargs.get("message", "reconnecting")
        self._reconnect_attempts += 1
        self._last_reconnect_time = time.monotonic()
        self._set_state(STATE_RECONNECT_WAIT)
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

    def _on_reconnect_stopped(self, *args: Any, _ws_generation: Optional[int] = None, **kwargs: Any) -> None:
        """SDK 'autoReconnectStopped' event. For a RETIRED generation this is
        the expected "Disabled by client." echo of our own teardown — dropped
        silently. For the current generation it means the SDK exhausted its
        bounded retry budget (or auth failed): we report RECONNECT_WAIT
        honestly (still expected to come back via token refresh/restart)
        instead of pretending to be merely 'disconnected' while _should_run
        is still True."""
        message = args[-1] if args else kwargs.get("message", "auto-reconnect stopped")

        if not self._is_current(_ws_generation):
            logger.debug("Ignoring stale WS auto-reconnect stop from generation %s: %s", _ws_generation, message)
            return

        with self._state_lock:
            if self._auth_failed:
                self._set_state_locked(STATE_FAILED)
            elif self._should_run:
                self._set_state_locked(STATE_RECONNECT_WAIT)
            else:
                self._set_state_locked(STATE_DISCONNECTED)

        logger.warning("WebSocket auto-reconnect stopped: %s", message)
