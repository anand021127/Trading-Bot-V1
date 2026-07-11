"""WebSocket router for live frontend updates."""

from typing import Any, Dict

from fastapi import APIRouter, WebSocket

from ..websocket import (
    manager,
    build_initial_state,
    build_price_update,
    get_broker_ws_status,
)

router = APIRouter()


@router.get("/feed/status")
async def feed_status() -> Dict[str, Any]:
    """Lightweight poll endpoint for the REAL Upstox v3 market-data feed status
    (connection_status, last_tick_time, reconnect_attempts, endpoint). This is
    the broker socket, not the frontend push channel."""
    return get_broker_ws_status()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        await manager.send_to(websocket, build_initial_state())
        while True:
            await websocket.receive_text()
    except Exception:
        pass
    finally:
        manager.disconnect(websocket)


async def broadcast_price_update() -> None:
    await manager.broadcast(build_price_update())
