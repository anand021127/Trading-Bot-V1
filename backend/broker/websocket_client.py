"""Minimal websocket client wrapper for broker streaming updates.

The implementation is intentionally lightweight so it can be used in tests and
later extended for real Upstox streaming connections.
"""

from __future__ import annotations

from typing import Optional, Protocol


class SupportsSocket(Protocol):
    """Protocol for the socket-like dependency used by the client."""

    def connect(self) -> None: ...
    def close(self) -> None: ...
    def send(self, message: str) -> None: ...


class WebSocketClient:
    """Simple wrapper around a websocket connection."""

    def __init__(self, socket: Optional[SupportsSocket] = None) -> None:
        self._socket = socket
        self.is_connected = False

    def connect(self) -> None:
        """Connect the underlying socket if one is configured."""
        if self._socket is not None:
            self._socket.connect()
            self.is_connected = True

    def send(self, message: str) -> None:
        """Send a message through the underlying socket when available."""
        if self._socket is not None:
            self._socket.send(message)

    def disconnect(self) -> None:
        """Disconnect from the underlying socket and clear the connected state."""
        if self._socket is not None:
            self._socket.close()
        self.is_connected = False
