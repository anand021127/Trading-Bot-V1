"""Unit tests for the Upstox REST client wrapper."""

from __future__ import annotations

import os
from typing import Any, Dict
from unittest import mock

import pytest

from backend.broker.upstox_client import UpstoxClient


class DummyResponse:
    """Simple response stub for testing."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> Dict[str, Any]:
        return self._payload


def test_client_uses_bearer_token_for_headers() -> None:
    """The client should attach the bearer token to requests."""
    client = UpstoxClient(access_token="token-abc")

    with mock.patch("backend.broker.upstox_client.requests.get") as mock_get:
        mock_get.return_value = DummyResponse({"status": "ok"})
        result = client.get("/v2/market-quote/quotes/NSE_EQ%3ANIFTY%2050")

    assert result["status"] == "ok"
    headers = mock_get.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer token-abc"
    assert headers["Content-Type"] == "application/json"


def test_client_raises_for_http_errors() -> None:
    """The client should raise a runtime error when the request fails."""
    client = UpstoxClient(access_token="token-abc")

    with mock.patch("backend.broker.upstox_client.requests.get") as mock_get:
        mock_get.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError, match="boom"):
            client.get("/v2/market-quote/quotes/NSE_EQ%3ANIFTY%2050")
