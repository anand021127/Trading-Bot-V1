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
