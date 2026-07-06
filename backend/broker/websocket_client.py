"""Upstox WebSocket client for live market data streaming.

Connects to wss://api.upstox.com/v2/feed/market-data-feed with
Authorization Bearer token header.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

UPSTOX_WS_URL = "wss://api.upstox.com/v2/feed/market-data-feed"


class UpstoxWebSocketClient:
    """
    Production WebSocket client for Upstox live data feed.

    - Authenticates via Bearer token in headers
    - Auto-reconnects with exponential backoff
    - Heartbeat monitoring (detects stale connections)
    - Thread-safe price cache
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
        self._reconnect_delay = 2.0
        self._last_message_time: float = 0.0
        self._should_run = False
        self._ws_thread: Optional[threading.Thread] = None

    def subscribe(self, instrument_keys: List[str]) -> None:
        """Add instrument keys to subscription."""
        self._subscribed_keys.update(instrument_keys)

    def get_latest_prices(self) -> Dict[str, Any]:
        """Thread-safe read of latest prices."""
        with self._prices_lock:
            return dict(self._prices)

    def get_price(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self._prices_lock:
            return self._prices.get(symbol)

    def is_data_stale(self, max_age_seconds: float = 30.0) -> bool:
        """Return True if no data received in max_age_seconds."""
        if self._last_message_time == 0:
            return True
        return time.monotonic() - self._last_message_time > max_age_seconds

    def start(self) -> None:
        """Start WebSocket in background thread."""
        if not self.access_token:
            logger.warning("No access token — WebSocket will not connect")
            return
        self._should_run = True
        self._ws_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._ws_thread.start()

    def stop(self) -> None:
        self._should_run = False
        self.is_connected = False

    def _run_loop(self) -> None:
        """Reconnect loop with exponential backoff."""
        while self._should_run:
            try:
                asyncio.run(self._connect())
            except Exception as e:
                logger.warning("WebSocket error: %s. Reconnecting in %.1fs", e, self._reconnect_delay)
                self.is_connected = False
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60.0)

    async def _connect(self) -> None:
        try:
            import websockets  # type: ignore
        except ImportError:
            logger.error("websockets package not installed")
            return

        headers = {"Authorization": f"Bearer {self.access_token}"}
        try:
            async with websockets.connect(
                UPSTOX_WS_URL,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                self.is_connected = True
                self._reconnect_delay = 2.0  # reset on success
                logger.info("WebSocket connected to Upstox")

                # Subscribe to instruments
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

                async for message in ws:
                    if not self._should_run:
                        break
                    self._last_message_time = time.monotonic()
                    self._handle_message(message)

        except Exception:
            self.is_connected = False
            raise

    def _handle_message(self, raw: Any) -> None:
        """Parse and cache incoming market data."""
        try:
            if isinstance(raw, bytes):
                import struct
                # Upstox sends protobuf or JSON depending on mode
                # For ltpc/full mode with JSON API
                try:
                    data = json.loads(raw.decode("utf-8"))
                except Exception:
                    return  # protobuf — skip for now
            else:
                data = json.loads(str(raw))

            feeds = data.get("feeds", {})
            for instrument_key, feed in feeds.items():
                ltpc = feed.get("ff", {}).get("marketFF", {}).get("ltpc", {})
                if ltpc:
                    ltp = float(ltpc.get("ltp", 0))
                    cp = float(ltpc.get("cp", ltp) or ltp)
                    change = ltp - cp
                    with self._prices_lock:
                        self._prices[instrument_key] = {
                            "ltp": ltp,
                            "close": cp,
                            "change": round(change, 2),
                            "change_pct": round((change / cp * 100) if cp else 0, 3),
                            "volume": int(feed.get("ff", {}).get("marketFF", {}).get("marketOHLC", {}).get("ohlc", [{}])[-1].get("vol", 0) if feed else 0),
                        }
                    if self._on_price_update:
                        self._on_price_update({"symbol": instrument_key, "ltp": ltp})
        except Exception as e:
            logger.debug("WS message parse error: %s", e)


# ── Simple stub for backward compatibility ────────────────────────────────────

class WebSocketClient:
    """Legacy stub — preserved for backward compatibility with existing tests."""

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
