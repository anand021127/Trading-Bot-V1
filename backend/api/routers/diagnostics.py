from fastapi import APIRouter
from typing import Dict, Any
import time
from datetime import datetime

router = APIRouter()

# Mock websocket health - in real use the websocket_client
ws_health = {
    "connected": False,
    "subscribed_count": 0,
    "last_tick": None,
    "last_error": None,
    "uptime_seconds": 0
}

@router.get("/diagnostics/websocket")
def get_websocket_diagnostics():
    """Get WebSocket health and status."""
    return {
        "websocket": {
            "connected": ws_health["connected"],
            "subscribed_count": ws_health["subscribed_count"],
            "last_tick": ws_health["last_tick"],
            "last_error": ws_health["last_error"],
            "uptime_seconds": ws_health["uptime_seconds"],
            "status": "connected" if ws_health["connected"] else "disconnected"
        }
    }

# Function to update from websocket (called from client)
def update_ws_health(connected: bool, subscribed: int = 0, last_tick=None, error=None):
    ws_health["connected"] = connected
    ws_health["subscribed_count"] = subscribed
    if last_tick:
        ws_health["last_tick"] = last_tick
    if error:
        ws_health["last_error"] = error
    ws_health["uptime_seconds"] = int(time.time() - 1720000000)  # mock
