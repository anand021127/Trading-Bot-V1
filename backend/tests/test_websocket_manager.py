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


def test_price_cache_survives_concurrent_writer_and_reader_threads() -> None:
    """Regression test: update_price_cache() is called from the Upstox
    SDK's own thread while get_prices_by_symbol() is read from other
    threads (request handlers, LiveScanner's asyncio.to_thread calls).
    Without a lock, concurrent dict mutation + iteration can raise
    'dictionary changed size during iteration'."""
    import threading
    from backend.api.websocket import update_price_cache, get_prices_by_symbol

    errors = []
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            update_price_cache({f"NSE_EQ|KEY{i % 50}": {"ltp": float(i)}})
            i += 1

    def reader():
        while not stop.is_set():
            try:
                get_prices_by_symbol()
            except RuntimeError as e:
                errors.append(e)

    threads = [threading.Thread(target=writer) for _ in range(3)] + \
              [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    import time
    time.sleep(0.5)
    stop.set()
    for t in threads:
        t.join(timeout=2)

    assert errors == []
