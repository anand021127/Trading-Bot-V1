"""Tests for the WebSocket connection manager."""

from __future__ import annotations

import asyncio

from backend.api.websocket import ConnectionManager, build_initial_state, build_price_update


class DummyWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def accept(self) -> None:
        return None

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


def test_connection_manager_connect_and_broadcast() -> None:
    async def run_test() -> None:
        manager = ConnectionManager()
        websocket = DummyWebSocket()
        await manager.connect(websocket)
        await manager.broadcast({"type": "test", "value": 1})

        assert websocket.sent == [{"type": "test", "value": 1}]

    asyncio.run(run_test())


def test_build_initial_state_contains_type() -> None:
    state = build_initial_state()
    assert state["type"] == "initial_state"
    assert "timestamp" in state


def test_build_price_update_contains_type() -> None:
    update = build_price_update()
    assert update["type"] == "price_update"
    assert "timestamp" in update


def test_payload_carries_feed_status_and_scanner() -> None:
    """Both message builders share one payload that must expose the real feed
    status block and the scanner snapshot for an honest dashboard."""
    for state in (build_initial_state(), build_price_update()):
        payload = state["payload"]
        assert "feed_status" in payload
        assert "last_tick_time" in payload
        assert "scanner" in payload
        # scanner snapshot has a stable shape even before the first scan
        assert set(payload["scanner"]).issuperset({"currently_scanning", "symbols"})
        assert "prices" in payload


def test_price_cache_by_symbol_preserves_enriched_fields() -> None:
    """get_prices_by_symbol must pass through prev_close/trend/volume so the
    Live Premiums page can render them."""
    from backend.api import websocket as ws_mod

    ws_mod.update_price_cache({
        "NSE_EQ|INE002A01018": {
            "ltp": 2530.0, "prev_close": 2500.0, "change": 30.0,
            "change_pct": 1.2, "trend": "up", "volume": 200000,
        }
    })
    by_symbol = ws_mod.get_prices_by_symbol()
    # instrument_key remaps to the friendly symbol RELIANCE
    quote = by_symbol.get("RELIANCE")
    assert quote is not None
    assert quote["prev_close"] == 2500.0
    assert quote["trend"] == "up"
    assert quote["volume"] == 200000
