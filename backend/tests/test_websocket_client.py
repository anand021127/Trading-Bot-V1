"""Unit tests for the Upstox v3 WebSocket client wrapper.

These test the client's own logic (parsing, caching, status reporting,
lifecycle) using synthetic protobuf-decoded dicts shaped exactly like what
`upstox_client.MarketDataStreamerV3` emits — they do NOT hit the real
Upstox feed or fabricate market prices.

Two groups:

  A. Parsing/status/caching unit tests (the original set).
  B. Lifecycle regression scenarios (`test_scenario_*`) covering the
     single-owner generation fence, the NoneType.sock class of bug,
     subscribe-only-after-open, STREAMING-only-on-real-message, reconnect
     token freshness, and the truthful API diagnostic.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import patch

from backend.broker.websocket_client import (
    STATE_CONNECTED,
    STATE_CONNECTING,
    STATE_DISCONNECTED,
    STATE_RECONNECT_WAIT,
    STATE_STREAMING,
    STATE_SUBSCRIBED,
    UpstoxWebSocketClient,
    _extract_ltpc,
    _extract_volume,
)


def _sample_option_feed(ltp: float, cp: float, vtt: int) -> dict:
    """Shape produced by json_format.MessageToDict(FeedResponse) for a
    'full' mode option-contract tick."""
    return {
        "fullFeed": {
            "marketFF": {
                "ltpc": {"ltp": ltp, "cp": cp, "ltt": "1700000000000", "ltq": "10"},
                "vtt": str(vtt),
            }
        }
    }


def _sample_index_feed(ltp: float, cp: float) -> dict:
    """Indices have no traded volume — indexFF carries no vtt field."""
    return {
        "fullFeed": {
            "indexFF": {
                "ltpc": {"ltp": ltp, "cp": cp, "ltt": "1700000000000", "ltq": "0"},
            }
        }
    }


def _tick_message(key: str, ltp: float, cp: float) -> dict:
    """A decoded FeedResponse with one real market-data feed entry."""
    return {"type": "live_feed", "feeds": {key: _sample_option_feed(ltp, cp, 1000)}}


# ═══════════════════════════════════════════════════════════════════════════
# Fake SDK streamer — models the parts of MarketDataStreamerV3 this client
# touches: event registry, connect(), disconnect(), auto_reconnect(),
# subscribe()/unsubscribe(). No network, no protobuf.
# ═══════════════════════════════════════════════════════════════════════════

class _FakeStreamer:
    instances: list = []

    def __init__(self, api_client=None, instrumentKeys=None, mode="full"):
        self.api_client = api_client
        self.instrument_keys = list(instrumentKeys or [])
        self.mode = mode
        self.handlers: dict = {}
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.auto_reconnect_calls: list = []
        self.subscribe_calls: list = []
        self.unsubscribe_calls: list = []
        type(self).instances.append(self)

    def on(self, event, listener):
        self.handlers.setdefault(event, []).append(listener)

    def emit(self, event, *args):
        for fn in self.handlers.get(event, []):
            fn(*args)

    def connect(self):
        self.connect_calls += 1

    def disconnect(self):
        self.disconnect_calls += 1

    def auto_reconnect(self, enable, interval=1, retry_count=5):
        self.auto_reconnect_calls.append((enable, interval, retry_count))

    def subscribe(self, keys, mode):
        self.subscribe_calls.append((list(keys), mode))

    def unsubscribe(self, keys):
        self.unsubscribe_calls.append(list(keys))


def _fake_sdk():
    """Patch upstox_client so _build_and_connect() constructs _FakeStreamer
    objects — fully offline, deterministic, and inspectable."""
    return patch.multiple(
        "upstox_client",
        Configuration=lambda: SimpleNamespace(),
        ApiClient=lambda cfg: SimpleNamespace(),
        MarketDataStreamerV3=_FakeStreamer,
    )


def _last_streamer() -> _FakeStreamer:
    assert _FakeStreamer.instances, "expected a fake streamer to have been built"
    return _FakeStreamer.instances[-1]


def _diagnostic_report(**over):
    """Shape returned by get_broker_ws_status()/status_report() for the
    diagnostic tests (keys the new _test_websocket mapping reads)."""
    base = dict(
        state="connected",
        connection_status="connected",
        streaming=False,
        is_connected=True,
        auth_failed=False,
        market_open=True,
        ticks_received=0,
        subscribed_instruments=6,
        last_tick_age_seconds=None,
        last_error=None,
        reconnect_attempts=0,
    )
    base.update(over)
    return base


# ═══════════════════════════════════════════════════════════════════════════
# A. Parsing / status / caching unit tests (original set — unchanged)
# ═══════════════════════════════════════════════════════════════════════════

def test_extract_ltpc_from_market_full_feed() -> None:
    feed = _sample_option_feed(ltp=2500.5, cp=2480.0, vtt=100000)
    ltpc = _extract_ltpc(feed)
    assert ltpc["ltp"] == 2500.5
    assert ltpc["cp"] == 2480.0


def test_extract_ltpc_from_index_full_feed() -> None:
    feed = _sample_index_feed(ltp=22100.0, cp=21980.0)
    assert _extract_ltpc(feed)["ltp"] == 22100.0


def test_extract_volume_present_for_equities() -> None:
    feed = _sample_option_feed(ltp=100, cp=99, vtt=54321)
    assert _extract_volume(feed) == 54321


def test_extract_volume_absent_for_indices_returns_zero() -> None:
    """Indices genuinely have no traded volume — 0 is correct, not a bug."""
    feed = _sample_index_feed(ltp=22100.0, cp=21980.0)
    assert _extract_volume(feed) == 0


def test_on_message_populates_price_cache_with_change_pct() -> None:
    client = UpstoxWebSocketClient(access_token="fake-token-for-unit-test-only")
    data = {
        "type": "live_feed",
        "currentTs": "1700000000000",
        "feeds": {
            "NSE_FO|OPTION": _sample_option_feed(ltp=2530.0, cp=2500.0, vtt=200000),
        },
    }
    client._on_message(None, data)

    price = client.get_price("NSE_FO|OPTION")
    assert price is not None
    assert price["ltp"] == 2530.0
    assert price["prev_close"] == 2500.0
    assert round(price["change"], 2) == 30.0
    assert round(price["change_pct"], 2) == 1.2
    assert price["volume"] == 200000


def test_on_message_ignores_market_info_ticks() -> None:
    client = UpstoxWebSocketClient(access_token="fake-token-for-unit-test-only")
    client._on_message(None, {"type": "market_info", "marketInfo": {}})
    assert client.get_latest_prices() == {}


def test_start_without_token_sets_auth_failed_status() -> None:
    # Force empty token and block any env/DB resolution so this unit test
    # stays hermetic even when other tests leave a token in process state.
    with patch("backend.broker.token_resolver.resolve_upstox_token", return_value=""), \
         patch.dict(os.environ, {"UPSTOX_ACCESS_TOKEN": ""}, clear=False):
        client = UpstoxWebSocketClient(access_token="")
        client.start()
        assert client.connection_status == "auth_failed"
        assert client.is_connected is False


def test_on_open_sets_connected_status() -> None:
    client = UpstoxWebSocketClient(access_token="fake-token-for-unit-test-only")
    client._on_open()
    assert client.connection_status == "connected"
    assert client.is_connected is True


def test_on_error_401_sets_auth_failed() -> None:
    client = UpstoxWebSocketClient(access_token="fake-token-for-unit-test-only")
    client._on_open()
    client._on_error(None, "401 Unauthorized")
    assert client.connection_status == "auth_failed"
    assert client.is_connected is False
    assert client._auth_failed is True
    assert client._should_run is False

    # Ensure subsequent close does not revert to reconnecting
    client._on_close(None, 1006, "abnormal closure")
    assert client.connection_status == "auth_failed"
    assert client.is_connected is False


def test_reconnect_with_token_resets_auth_failed() -> None:
    client = UpstoxWebSocketClient(access_token="fake-token-for-unit-test-only")
    client._on_open()
    client._on_error(None, "401 Unauthorized")
    assert client._auth_failed is True

    # Reconnect with a valid new mock token
    new_token = "valid-new-active-token-mock-999"
    # Mock _build_and_connect to avoid actual network call
    called = []
    client._build_and_connect = lambda: called.append(True)

    client.reconnect_with_token(new_token)
    assert client._auth_failed is False
    assert client.connection_status == "connecting"
    assert client.access_token == new_token
    assert len(called) == 1


def test_reconnect_with_expired_token_rejected() -> None:
    import base64
    import json
    import time

    h = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).decode().rstrip("=")
    p = base64.urlsafe_b64encode(json.dumps({"user_id": "U1", "exp": time.time() - 3600}).encode()).decode().rstrip("=")
    expired_jwt = f"{h}.{p}.sig"

    client = UpstoxWebSocketClient(access_token="fake-token-for-unit-test-only")
    client._build_and_connect = lambda: (_ for _ in ()).throw(AssertionError("Should not connect"))
    client.reconnect_with_token(expired_jwt)
    assert client._auth_failed is True
    assert client.connection_status == "auth_failed"


def test_bounded_exponential_backoff() -> None:
    client = UpstoxWebSocketClient(access_token="fake-token-for-unit-test-only")
    client._should_run = True
    assert client._backoff_delay == client._base_backoff

    for attempt in range(1, 10):
        client._on_reconnecting("reconnecting")
        client._on_close(None, 1006, "test")
        assert client._backoff_delay <= client._max_backoff
        assert client._backoff_delay >= client._base_backoff


def test_on_close_sets_reconnecting_when_should_run() -> None:
    client = UpstoxWebSocketClient(access_token="fake-token-for-unit-test-only")
    client._should_run = True
    client._on_open()
    client._on_close(None, 1006, "abnormal closure")
    assert client.connection_status == "reconnecting"
    assert client.is_connected is False


def test_is_data_stale_true_before_any_tick() -> None:
    client = UpstoxWebSocketClient(access_token="fake-token-for-unit-test-only")
    assert client.is_data_stale() is True


def test_status_report_shape() -> None:
    client = UpstoxWebSocketClient(
        access_token="fake-token-for-unit-test-only",
        instrument_keys=["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank"],
    )
    report = client.status_report()
    assert report["subscribed_instruments"] == 2
    assert report["feed_endpoint"] == "wss://api.upstox.com/v3/feed/market-data-feed"
    assert "connection_status" in report


# ═══════════════════════════════════════════════════════════════════════════
# B. Lifecycle regression scenarios (the 18 required behaviors)
# ═══════════════════════════════════════════════════════════════════════════

def test_scenario_01_start_with_valid_token_creates_exactly_one_connection() -> None:
    """1. WebSocket starts with a valid token: builds ONE streamer, enables
    the SDK's bounded auto-reconnect (single reconnect owner), CONNECTING
    until the handshake callback, then CONNECTED → SUBSCRIBED."""
    _FakeStreamer.instances.clear()
    client = UpstoxWebSocketClient(
        access_token="mock-fresh-start-token",
        instrument_keys=["NSE_INDEX|Nifty 50"],
    )
    with _fake_sdk():
        client.start()
        assert client._get_state() == STATE_CONNECTING
        assert client._should_run is True
        fs = _last_streamer()
        assert fs.connect_calls == 1
        assert len(_FakeStreamer.instances) == 1  # exactly ONE owner object
        assert fs.auto_reconnect_calls and fs.auto_reconnect_calls[0][0] is True
        assert client._get_state() == STATE_CONNECTING  # handshake not confirmed yet

        fs.emit("open")
    assert client._get_state() == STATE_SUBSCRIBED
    assert client.is_connected is True
    assert client.connection_status == "connected"


def test_scenario_02_open_callback_transitions_to_connected() -> None:
    """2. Receiving the open callback moves the machine to CONNECTED and the
    legacy connection_status stays truthful."""
    client = UpstoxWebSocketClient(access_token="fake-token-for-unit-test-only")
    assert client._get_state() == STATE_DISCONNECTED
    client._on_open()
    assert client._get_state() == STATE_CONNECTED
    assert client.is_connected is True


def test_scenario_03_subscription_sent_only_after_open() -> None:
    """3. No subscribe frame may ever be sent before the socket is open —
    keys added while CONNECTING are buffered and sent by the open callback."""
    _FakeStreamer.instances.clear()
    client = UpstoxWebSocketClient(
        access_token="mock-token", instrument_keys=["NSE_INDEX|Nifty 50"],
    )
    with _fake_sdk():
        client.start()
        fs = _last_streamer()
        assert fs.subscribe_calls == []  # nothing during CONNECTING

        client.subscribe(["NSE_FO|ADDED"])  # buffered into the desired set
        assert "NSE_FO|ADDED" in client._instrument_keys
        assert fs.subscribe_calls == []     # still nothing — socket NOT open

        fs.emit("open")
    assert len(fs.subscribe_calls) == 1
    keys, mode = fs.subscribe_calls[0]
    assert "NSE_INDEX|Nifty 50" in keys and "NSE_FO|ADDED" in keys
    assert mode == "full"


def test_scenario_04_six_resolved_instruments_are_subscribed() -> None:
    """4. All six resolved index instruments are subscribed after open."""
    from backend.broker.upstox_client import ALL_INSTRUMENTS
    _FakeStreamer.instances.clear()
    client = UpstoxWebSocketClient(
        access_token="mock-token", instrument_keys=list(ALL_INSTRUMENTS.values()),
    )
    with _fake_sdk():
        client.start()
        fs = _last_streamer()
        assert len(client._instrument_keys) == 6
        fs.emit("open")
    assert len(fs.subscribe_calls) == 1
    keys, _mode = fs.subscribe_calls[0]
    assert set(keys) == set(ALL_INSTRUMENTS.values())
    assert len(keys) == 6


def test_scenario_05_first_market_data_message_sets_streaming() -> None:
    """5. STREAMING is earned ONLY by the first real market-data message —
    connected/subscribed alone can never produce it."""
    client = UpstoxWebSocketClient(access_token="fake-token-for-unit-test-only")
    client._on_open()
    assert client._get_state() == STATE_CONNECTED
    assert client.status_report()["streaming"] is False

    client._on_message(None, _tick_message("NSE_FO|K", 120.0, 100.0))
    assert client._get_state() == STATE_STREAMING
    assert client.status_report()["streaming"] is True
    assert client.get_price("NSE_FO|K")["ltp"] == 120.0


def test_scenario_06_unexpected_close_enters_reconnect_wait() -> None:
    """6. Unexpected close while the client should run → RECONNECT_WAIT
    (reported as legacy 'reconnecting'), never a silent dead socket."""
    client = UpstoxWebSocketClient(access_token="fake-token-for-unit-test-only")
    client._should_run = True
    client._on_open()
    client._on_message(None, _tick_message("NSE_FO|K", 120.0, 100.0))
    assert client._get_state() == STATE_STREAMING

    client._on_close(None, 1006, "abnormal closure")
    assert client._get_state() == STATE_RECONNECT_WAIT
    assert client.connection_status == "reconnecting"
    assert client.is_connected is False
    assert client._should_run is True


def test_scenario_07_reconnect_occurs_after_unexpected_close() -> None:
    """7. A single reconnect owner exists: the SDK auto-reconnect is enabled
    exactly once with bounded interval/retry — we never layer a second
    competing reconnect loop."""
    _FakeStreamer.instances.clear()
    client = UpstoxWebSocketClient(
        access_token="mock-token", instrument_keys=["NSE_INDEX|Nifty 50"],
    )
    with _fake_sdk():
        client.start()
        fs = _last_streamer()
        fs.emit("open")
        fs.emit("close", None, None)  # unexpected — no stop() was called

    assert client._get_state() == STATE_RECONNECT_WAIT
    assert client._should_run is True
    assert client._current_streamer() is fs          # same generation recovers
    assert fs.auto_reconnect_calls == [(True, 2, 15)]  # bounded, enabled once


def test_scenario_08_reconnect_uses_latest_token() -> None:
    """8. The token-refresh rebuild connects with the NEW token, retires the
    old generation, and never logs the token itself."""
    _FakeStreamer.instances.clear()
    client = UpstoxWebSocketClient(access_token="mock-old-token-aaaa")
    with _fake_sdk():
        client.start()
        old_fs = _last_streamer()
        old_gen = client._current_generation()

        seen = {}
        real_build = client._build_and_connect

        def spy_build():
            seen["token"] = client.access_token
            real_build()

        client._build_and_connect = spy_build
        client.reconnect_with_token("mock-new-token-bbbb")

    assert client.access_token == "mock-new-token-bbbb"
    assert seen["token"] == "mock-new-token-bbbb"      # new build saw the NEW token
    assert client._current_generation() == old_gen + 1
    assert old_fs.disconnect_calls >= 1                # old socket invalidated
    new_fs = _last_streamer()
    assert new_fs is not old_fs
    assert new_fs.connect_calls == 1


def test_scenario_09_no_duplicate_reconnect_connections() -> None:
    """9. Concurrent/double reconnect triggers (OAuth double-fire) produce
    exactly ONE rebuild, and a healthy same-token refresh rebuilds nothing."""
    _FakeStreamer.instances.clear()
    client = UpstoxWebSocketClient(
        access_token="mock-token-1", instrument_keys=["NSE_INDEX|Nifty 50"],
    )
    with _fake_sdk():
        client.start()
        fs = _last_streamer()
        fs.emit("open")
        assert fs.connect_calls == 1

        # Same token + healthy generation → no rebuild at all
        with patch.object(client, "_build_and_connect") as p:
            client.reconnect_with_token(client.access_token)
        p.assert_not_called()
        assert fs.connect_calls == 1
        assert len(_FakeStreamer.instances) == 1

        # Deliberate refresh with a NEW token, fired twice (invalidate +
        # restart paths) → build triggered exactly once (the second same-token
        # call hits the idempotence guard against the CONNECTING generation).
        gen_before = client._current_generation()
        with patch.object(client, "_build_and_connect") as p2:
            client.reconnect_with_token("mock-token-2")
            client.reconnect_with_token("mock-token-2")
        assert p2.call_count == 1
        assert client._current_generation() == gen_before + 1
        assert client._get_state() == STATE_CONNECTING
        assert client.access_token == "mock-token-2"


def test_scenario_10_stale_generation_callbacks_are_fenced() -> None:
    """10. THE NoneType.sock regression: after a token-refresh rotation the
    SDK's retired run_forever thread still fires callbacks. Every stale
    event (open/message/error/close/reconnecting/autoReconnectStopped) is
    dropped — it can never mutate state, prices, or subscriptions."""
    _FakeStreamer.instances.clear()
    client = UpstoxWebSocketClient(
        access_token="mock-token-old", instrument_keys=["NSE_INDEX|Nifty 50"],
    )
    with _fake_sdk():
        client.start()
        old_fs = _last_streamer()
        old_gen = client._current_generation()
        old_fs.emit("open")
        old_fs.emit("message", _tick_message("NSE_FO|LIVE", 100.0, 90.0))
        assert client._get_state() == STATE_STREAMING

        client.reconnect_with_token("mock-token-new")
        new_fs = _last_streamer()
        assert client._current_generation() == old_gen + 1
        assert client._get_state() == STATE_CONNECTING

        # The retired socket thread now wakes up and fires EVERYTHING —
        # including the exact AttributeError the journal recorded.
        old_fs.emit("open")
        old_fs.emit("message", _tick_message("NSE_FO|GHOST", 1.0, 1.0))
        old_fs.emit("error", AttributeError("'NoneType' object has no attribute 'sock'"))
        old_fs.emit("close", None, None)
        old_fs.emit("reconnecting")
        old_fs.emit("autoReconnectStopped", "Disabled by client.")

        assert client._current_generation() == old_gen + 1  # nothing bumped
        assert client._get_state() == STATE_CONNECTING      # new gen untouched
        assert "NSE_FO|GHOST" not in client.get_latest_prices()
        assert client._auth_failed is False

        # The new generation still works normally afterwards.
        new_fs.emit("open")
        assert client._get_state() == STATE_SUBSCRIBED


def test_scenario_11_socket_none_never_raises_none_type_sock() -> None:
    """10/11. With NO socket at all (streamer None — exactly the state that
    produced "'NoneType' object has no attribute 'sock'"), every lifecycle
    handler, subscribe/unsubscribe, and stop() completes safely."""
    client = UpstoxWebSocketClient(access_token="fake-token-for-unit-test-only")
    client._on_open()
    client._on_message(None, {"feeds": {}})
    client._on_message(None, {"type": "market_info", "marketInfo": {}})
    client._on_error(None, "boom")
    client._on_close(None, None, None)
    client._on_reconnecting("try")
    client._on_reconnect_stopped("Disabled by client.")
    client.subscribe(["NSE_FO|X"])     # buffered, never sent
    client.unsubscribe(["NSE_FO|X"])
    client.stop()
    client.stop()
    assert client.get_latest_prices() == {}
    assert client._get_state() == STATE_DISCONNECTED

    # The literal reported exception string must be recordable without any
    # socket access and must not corrupt the machine.
    client._on_error(None, AttributeError("'NoneType' object has no attribute 'sock'"))
    assert client._last_error == "'NoneType' object has no attribute 'sock'"


def test_scenario_12_closed_socket_is_not_reused() -> None:
    """11/12. A socket that raises 'WebSocket is not open.' mid-subscribe is
    never retried on the dead object; the error is swallowed, state survives,
    and after close nothing is sent until the socket reopens."""
    _FakeStreamer.instances.clear()
    client = UpstoxWebSocketClient(
        access_token="mock-token", instrument_keys=["NSE_INDEX|Nifty 50"],
    )
    with _fake_sdk():
        client.start()
        fs = _last_streamer()
        fs.emit("open")
        fs.emit("message", _tick_message("NSE_INDEX|Nifty 50", 22000.0, 21900.0))
        assert client._get_state() == STATE_STREAMING

        attempted = []

        def dead_socket_subscribe(keys, mode=None):
            attempted.append(list(keys))
            raise Exception("WebSocket is not open.")

        fs.subscribe = dead_socket_subscribe
        # Socket dies mid-call → exception swallowed, no raise, no state corruption
        client.subscribe(["NSE_FO|NEW"])
        assert attempted == [["NSE_FO|NEW"]]
        assert client._get_state() == STATE_STREAMING
        assert "NSE_FO|NEW" in client._instrument_keys

        fs.emit("close", None, None)
        assert client._get_state() == STATE_RECONNECT_WAIT

        # Closed socket is NOT reused: nothing is attempted while disconnected
        client.subscribe(["NSE_FO|AFTER"])
        assert attempted == [["NSE_FO|NEW"]]


def test_scenario_13_cleanup_is_idempotent() -> None:
    """12. stop() and streamer teardown can be called any number of times in
    any state without erroring, double-closing, or flipping state backwards."""
    _FakeStreamer.instances.clear()
    client = UpstoxWebSocketClient(
        access_token="mock-token", instrument_keys=["NSE_INDEX|Nifty 50"],
    )
    with _fake_sdk():
        client.start()
        fs = _last_streamer()
        fs.emit("open")
        d0 = fs.disconnect_calls

        client.stop()
        client.stop()
        client.stop()
        assert client._get_state() == STATE_DISCONNECTED
        assert fs.disconnect_calls == d0 + 1          # torn down exactly once
        assert client._current_streamer() is None

        # Tearing an already-retired streamer down again is harmless
        client._teardown_streamer(fs)
        client._teardown_streamer(None)
        assert client._get_state() == STATE_DISCONNECTED
        assert fs.disconnect_calls == d0 + 2          # second teardown also ran, no error

        # A late close after deliberate stop stays DISCONNECTED
        fs.emit("close", 1000, "normal")
        assert client._get_state() == STATE_DISCONNECTED
        assert client.connection_status == "disconnected"


def test_scenario_14_token_refresh_invalidates_old_websocket() -> None:
    """13. Token refresh retires the old WebSocket (SDK reconnect disabled,
    disconnect called) and its late callbacks cannot resurrect it."""
    _FakeStreamer.instances.clear()
    client = UpstoxWebSocketClient(
        access_token="mock-token-old", instrument_keys=["NSE_INDEX|Nifty 50"],
    )
    with _fake_sdk():
        client.start()
        old_fs = _last_streamer()
        old_fs.emit("open")
        old_fs.emit("message", _tick_message("NSE_INDEX|Nifty 50", 22000.0, 21900.0))
        assert client._get_state() == STATE_STREAMING

        client.reconnect_with_token("mock-token-new-xyz")

        assert old_fs.disconnect_calls >= 1
        assert old_fs.auto_reconnect_calls[-1][0] is False  # SDK reconnect OFF for retired gen
        assert client._current_streamer() is not old_fs

        # Late close from the retired generation changes nothing
        old_fs.emit("close", None, None)
        assert client._get_state() == STATE_CONNECTING
        assert client.is_connected is False


def test_scenario_15_diagnostic_never_passes_without_real_message() -> None:
    """14. The WebSocket API diagnostic must NOT report PASS for
    connected/subscribed states without a received market-data message —
    and must still PASS when truly STREAMING."""
    import backend.api.routers.diagnostics as diag

    def run(report):
        with patch("backend.broker.token_resolver.resolve_upstox_token",
                   return_value="mock-token-long-enough"), \
             patch("backend.api.websocket.get_broker_ws_status", return_value=report):
            return asyncio.run(diag._test_websocket())

    # CONNECTED, market open, zero ticks → WARN, never PASS
    res = run(_diagnostic_report(state="connected"))
    assert res["status"] == "WARN"
    assert "no market-data message" in res["details"]

    # SUBSCRIBED, zero ticks → WARN, never PASS
    res = run(_diagnostic_report(state="subscribed"))
    assert res["status"] == "WARN"
    assert "no market-data message" in res["details"]

    # Positive control: genuinely streaming → PASS
    res = run(_diagnostic_report(state="streaming", streaming=True, ticks_received=3,
                                 last_tick_age_seconds=0.4))
    assert res["status"] == "PASS"
    assert "STREAMING" in res["details"]


def test_scenario_16_diagnostic_reports_reconnecting() -> None:
    """15. RECONNECTING/RECONNECT_WAIT is reported as WARN with the attempt
    count — never PASS, never fabricated as connected."""
    import backend.api.routers.diagnostics as diag

    def run(report):
        with patch("backend.broker.token_resolver.resolve_upstox_token",
                   return_value="mock-token-long-enough"), \
             patch("backend.api.websocket.get_broker_ws_status", return_value=report):
            return asyncio.run(diag._test_websocket())

    res = run(_diagnostic_report(state="reconnect_wait", connection_status="reconnecting",
                                 is_connected=False, reconnect_attempts=3))
    assert res["status"] == "WARN"
    assert "reconnect_wait" in res["details"]
    assert "3" in res["details"]

    # Legacy status shape (no explicit state key) still maps correctly
    legacy = _diagnostic_report()
    legacy.pop("state")
    res = run(legacy)
    assert res["status"] == "WARN"


def test_scenario_17_market_closed_is_not_reported_as_streaming() -> None:
    """16. A connected-but-market-closed feed is honestly WARN — explicitly
    NOT streaming, never a PASS."""
    import backend.api.routers.diagnostics as diag

    with patch("backend.broker.token_resolver.resolve_upstox_token",
               return_value="mock-token-long-enough"), \
         patch("backend.api.websocket.get_broker_ws_status",
               return_value=_diagnostic_report(state="connected", market_open=False)):
        res = asyncio.run(diag._test_websocket())

    assert res["status"] == "WARN"
    assert "market is closed" in res["details"]
    assert "NOT" in res["details"]


def test_scenario_18_subscription_is_dynamic_from_instrument_master() -> None:
    """17. Subscription is always exactly the resolved instrument list —
    six keys in production, and any other resolved set verbatim (nothing
    hardcoded)."""
    from backend.broker.upstox_client import ALL_INSTRUMENTS
    _FakeStreamer.instances.clear()

    client = UpstoxWebSocketClient(
        access_token="mock-token", instrument_keys=list(ALL_INSTRUMENTS.values()),
    )
    assert client._instrument_keys == list(ALL_INSTRUMENTS.values())
    assert len(client._instrument_keys) == 6
    with _fake_sdk():
        client.start()
        fs = _last_streamer()
        fs.emit("open")
    keys, _mode = fs.subscribe_calls[0]
    assert sorted(keys) == sorted(ALL_INSTRUMENTS.values())

    # A differently-resolved universe flows through untouched
    alt = ["BSE_INDEX|SENSEX", "NSE_INDEX|Nifty Bank"]
    client2 = UpstoxWebSocketClient(access_token="mock-token", instrument_keys=alt)
    with _fake_sdk():
        client2.start()
        fs2 = _last_streamer()
        fs2.emit("open")
    keys2, _mode2 = fs2.subscribe_calls[0]
    assert keys2 == alt


def test_auth_and_rest_surface_unchanged() -> None:
    """18. The legacy auth/REST contract is intact: connection_status
    vocabulary, is_connected semantics, status_report keys, and the 401
    auth-failure path all behave exactly as before the state machine."""
    client = UpstoxWebSocketClient(access_token="fake-token-for-unit-test-only")
    # DISCONNECTED → legacy 'disconnected'
    assert client.connection_status == "disconnected"
    assert client.is_connected is False
    # CONNECTING → legacy 'connecting'
    client._set_state(STATE_CONNECTING)
    assert client.connection_status == "connecting"
    assert client.is_connected is False
    # CONNECTED → legacy 'connected'
    client._set_state(STATE_CONNECTED)
    assert client.connection_status == "connected"
    assert client.is_connected is True
    # RECONNECT_WAIT → legacy 'reconnecting'
    client._set_state(STATE_RECONNECT_WAIT)
    assert client.connection_status == "reconnecting"
    # 401 path
    client._on_open()
    client._on_error(None, "401 Unauthorized")
    assert client.connection_status == "auth_failed"
    assert client._auth_failed is True
    # status_report exposes BOTH vocabularies
    report = client.status_report()
    for key in ("state", "streaming", "connection_status", "is_connected",
                "auth_failed", "auth_status", "token_fingerprint", "market_open",
                "market_data_status", "subscribed_instruments", "instrument_keys",
                "last_tick_age_seconds", "last_message_age_seconds", "is_stale",
                "last_error", "reconnect_attempts", "total_messages_received",
                "ticks_received", "ignored_messages_count", "parse_errors_count",
                "last_reconnect_seconds_ago", "feed_endpoint"):
        assert key in report
