"""Unit tests for the Upstox v3 WebSocket client wrapper.

These test the client's own logic (parsing, caching, status reporting,
lifecycle) using synthetic protobuf-decoded dicts shaped exactly like what
`upstox_client.MarketDataStreamerV3` emits — they do NOT hit the real
Upstox feed or fabricate market prices.
"""
from __future__ import annotations

from backend.broker.websocket_client import (
    UpstoxWebSocketClient,
    _extract_ltpc,
    _extract_volume,
)


def _sample_stock_feed(ltp: float, cp: float, vtt: int) -> dict:
    """Shape produced by json_format.MessageToDict(FeedResponse) for a
    'full' mode equity tick."""
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


def test_extract_ltpc_from_market_full_feed() -> None:
    feed = _sample_stock_feed(ltp=2500.5, cp=2480.0, vtt=100000)
    ltpc = _extract_ltpc(feed)
    assert ltpc["ltp"] == 2500.5
    assert ltpc["cp"] == 2480.0


def test_extract_ltpc_from_index_full_feed() -> None:
    feed = _sample_index_feed(ltp=22100.0, cp=21980.0)
    ltpc = _extract_ltpc(feed)
    assert ltpc["ltp"] == 22100.0


def test_extract_volume_present_for_equities() -> None:
    feed = _sample_stock_feed(ltp=100, cp=99, vtt=54321)
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
            "NSE_EQ|INE002A01018": _sample_stock_feed(ltp=2530.0, cp=2500.0, vtt=200000),
        },
    }
    client._on_message(None, data)

    price = client.get_price("NSE_EQ|INE002A01018")
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
    assert report["feed_version"] == "v3"
    assert "connection_status" in report
    # last_tick_time is None until the first tick, then a wall-clock ISO string.
    assert report["last_tick_time"] is None
    assert report["total_ticks"] == 0


def test_on_message_sets_trend_and_tick_time() -> None:
    client = UpstoxWebSocketClient(access_token="fake-token-for-unit-test-only")
    client._on_message(None, {
        "type": "live_feed",
        "feeds": {"NSE_EQ|INE002A01018": _sample_stock_feed(ltp=2530.0, cp=2500.0, vtt=200000)},
    })
    price = client.get_price("NSE_EQ|INE002A01018")
    assert price["trend"] == "up"          # ltp > prev_close
    assert price["tick_time"] is not None  # wall-clock ISO stamped on every tick

    # A down tick flips trend to "down".
    client._on_message(None, {
        "type": "live_feed",
        "feeds": {"NSE_EQ|INE002A01018": _sample_stock_feed(ltp=2470.0, cp=2500.0, vtt=210000)},
    })
    assert client.get_price("NSE_EQ|INE002A01018")["trend"] == "down"

    # status_report now reflects a real last_tick_time + tick count.
    report = client.status_report()
    assert report["last_tick_time"] is not None
    assert report["total_ticks"] == 2
