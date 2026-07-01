"""Unit tests for the Upstox websocket client wrapper."""

from __future__ import annotations

from unittest import mock

import pytest

from backend.broker.websocket_client import WebSocketClient


class DummyWebSocket:
    """Small stub for websocket interactions."""

    def __init__(self) -> None:
        self.connected = False
        self.closed = False
        self.messages: list[str] = []

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.closed = True

    def send(self, message: str) -> None:
        self.messages.append(message)


def test_connect_marks_client_as_connected() -> None:
    """Connecting a websocket should set the client state to connected."""
    client = WebSocketClient()
    socket = DummyWebSocket()

    client._socket = socket
    client.connect()

    assert client.is_connected is True
    assert socket.connected is True


def test_send_message_forwards_to_socket() -> None:
    """Sending a message should delegate to the configured socket."""
    client = WebSocketClient()
    socket = DummyWebSocket()

    client._socket = socket
    client.send("hello")

    assert socket.messages == ["hello"]


def test_disconnect_closes_socket() -> None:
    """Disconnecting should close the underlying socket if present."""
    client = WebSocketClient()
    socket = DummyWebSocket()

    client._socket = socket
    client.disconnect()

    assert socket.closed is True
    assert client.is_connected is False
