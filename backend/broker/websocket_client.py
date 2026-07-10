"""Upstox WebSocket client — uses authorized URI from Upstox v2 authorize endpoint."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

import requests

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://api.upstox.com/v3/feed/market-data-feed/authorize"


def get_authorized_ws_uri(token: str) -> Optional[str]:
    """
    Step 1 of Upstox v2 WebSocket flow:
    Call the authorize endpoint to get the real WebSocket URI.
    Returns the authorizedRedirectUri or None on failure.
    """
    try:
        r = requests.get(
            AUTHORIZE_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            uri = (data.get("data", {}).get("authorizedRedirectUri")
                   or data.get("authorizedRedirectUri"))
            return uri
        logger.warning("WS authorize failed: HTTP %d — %s", r.status_code, r.text[:200])
        return None
    except Exception as e:
        logger.error("WS authorize error: %s", e)
        return None


class UpstoxWebSocketClient:
    """
    Production WebSocket client using the Upstox v2 authorized URI flow.

    Flow:
      1. GET /feed/market-data-feed/authorize → authorizedRedirectUri
      2. Connect to that URI (auth embedded in URI)
      3. Send subscription JSON
      4. Process incoming market data ticks
      5. Auto-reconnect with exponential backoff
    """

    def __init__(
        self,
        access_token: Optional[str] = None,
        on_price_update: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.access_token = access_token or os.getenv("UPSTOX_ACCESS_TOKEN", "")
        self._on_price_update = on_price_update
        self._prices: Dict[str, Any] = {}
        self._prices_lock = threading.Lock()
        self._subscribed_keys: Set[str] = set()
        self.is_connected = False
        self.connection_status = "disconnected"  # disconnected | connecting | connected | reconnecting
        self._reconnect_delay = 2.0
        self._last_message_time: float = 0.0
        self._should_run = False
        self._ws_thread: Optional[threading.Thread] = None

    def subscribe(self, instrument_keys: List[str]) -> None:
        self._subscribed_keys.update(instrument_keys)

    def get_latest_prices(self) -> Dict[str, Any]:
        with self._prices_lock:
            return dict(self._prices)

    def get_price(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self._prices_lock:
            return self._prices.get(symbol)

    def is_data_stale(self, max_age_seconds: float = 30.0) -> bool:
        if self._last_message_time == 0:
            return True
        return time.monotonic() - self._last_message_time > max_age_seconds

    def start(self) -> None:
        if not self.access_token:
            logger.warning("No access token — WebSocket will not start")
            return
        self._should_run = True
        self._ws_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._ws_thread.start()

    def stop(self) -> None:
        self._should_run = False
        self.is_connected = False
        self.connection_status = "disconnected"

    def _run_loop(self) -> None:
        while self._should_run:
            try:
                self.connection_status = "connecting"
                asyncio.run(self._connect())
            except Exception as e:
                logger.warning("WS error: %s. Reconnecting in %.1fs", e, self._reconnect_delay)
                self.is_connected = False
                self.connection_status = "reconnecting"
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60.0)

    async def _connect(self) -> None:
        try:
            import websockets  # type: ignore
        except ImportError:
            logger.error("websockets package not installed")
            return

        # Step 1: Get authorized WebSocket URI
        ws_uri = get_authorized_ws_uri(self.access_token)
        if not ws_uri:
            logger.error("Could not get authorized WebSocket URI — token may be expired")
            self.connection_status = "disconnected"
            raise RuntimeError("Failed to get authorized WebSocket URI")

        # Step 2: Connect (no extra headers needed — auth is embedded in URI)
        try:
            async with websockets.connect(
                ws_uri,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                self.is_connected = True
                self.connection_status = "connected"
                self._reconnect_delay = 2.0
                logger.info("WebSocket connected via authorized URI")

                # Step 3: Subscribe to instruments
                if self._subscribed_keys:
                    sub_msg = json.dumps({
                        "guid": "upstox-bot",
                        "method": "sub",
                        "data": {
                            "mode": "full",
                            "instrumentKeys": list(self._subscribed_keys),
                        },
                    })
                    await ws.send(sub_msg)

                # Step 4: Process messages
                async for message in ws:
                    if not self._should_run:
                        break
                    self._last_message_time = time.monotonic()
                    self._handle_message(message)

        except Exception:
            self.is_connected = False
            self.connection_status = "reconnecting"
            raise

    def _handle_message(self, raw: Any) -> None:
        """Parse Upstox market data feed messages."""
        try:
            if isinstance(raw, bytes):
                try:
                    data = json.loads(raw.decode("utf-8"))
                except Exception:
                    return  # binary/protobuf frame — skip
            else:
                data = json.loads(str(raw))

            feeds = data.get("feeds", {})
            for instrument_key, feed in feeds.items():
                ltpc = (feed.get("ff", {})
                            .get("marketFF", {})
                            .get("ltpc", {}))
                if ltpc:
                    ltp = float(ltpc.get("ltp", 0))
                    cp  = float(ltpc.get("cp", ltp) or ltp)
                    change = ltp - cp
                    with self._prices_lock:
                        self._prices[instrument_key] = {
                            "ltp": ltp,
                            "close": cp,
                            "change": round(change, 2),
                            "change_pct": round((change / cp * 100) if cp else 0, 3),
                            "volume": 0,
                            "last_tick": time.monotonic(),
                        }
                    if self._on_price_update:
                        self._on_price_update({"symbol": instrument_key, "ltp": ltp})
        except Exception as e:
            logger.debug("WS message parse error: %s", e)


# Legacy stub for backward compat
class WebSocketClient:
    def __init__(self, socket=None) -> None:
        self._socket = socket
        self.is_connected = False

    def connect(self) -> None:
        if self._socket is not None:
            self._socket.connect()
            self.is_connected = True

    def send(self, message: str) -> None:
        if self._socket is not None:
            self._socket.send(message)

    def disconnect(self) -> None:
        if self._socket is not None:
            self._socket.close()
        self.is_connected = False
