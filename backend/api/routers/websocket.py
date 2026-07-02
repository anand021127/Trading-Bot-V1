"""WebSocket router for live frontend updates."""

from fastapi import APIRouter, WebSocket

from backend.api.websocket import manager, build_initial_state, build_price_update

router = APIRouter()


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
