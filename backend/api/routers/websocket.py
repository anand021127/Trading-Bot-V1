from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import json
import asyncio

router = APIRouter()

active_connections = []

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    try:
        while True:
            # Keep alive + handle client messages
            data = await websocket.receive_text()
            # Echo or process
            await websocket.send_text(json.dumps({
                "type": "price_update", 
                "payload": {"prices": {}}  # Mock - replace with real data later
            }))
    except WebSocketDisconnect:
        active_connections.remove(websocket)
    except Exception:
        if websocket in active_connections:
            active_connections.remove(websocket)