"""WebSocket connection manager and update helpers."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, Set

from fastapi import WebSocket


class ConnectionManager:
    """Manage active WebSocket connections for live updates."""

    def __init__(self) -> None:
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self.active_connections.discard(websocket)

    async def send_to(self, websocket: WebSocket, data: Dict[str, Any]) -> None:
        await websocket.send_json(data)

    async def broadcast(self, data: Dict[str, Any]) -> None:
        if not self.active_connections:
            return

        tasks = [self.send_to(connection, data) for connection in list(self.active_connections)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for websocket, result in zip(list(self.active_connections), results):
            if isinstance(result, Exception):
                self.disconnect(websocket)


manager = ConnectionManager()


def build_price_update() -> Dict[str, Any]:
    return {
        "type": "price_update",
        "timestamp": datetime.now().astimezone().isoformat(),
        "payload": {
            "market_open": False,
            "mode": "paper",
            "websocket_connected": len(manager.active_connections) > 0,
            "active_connections": len(manager.active_connections),
            "positions": [],
        },
    }


def build_initial_state() -> Dict[str, Any]:
    return {
        "type": "state",
        "timestamp": datetime.now().astimezone().isoformat(),
        "payload": {
            "mode": "paper",
            "market_open": False,
            "websocket_connected": len(manager.active_connections) > 0,
            "active_connections": len(manager.active_connections),
            "positions": [],
        },
    }
