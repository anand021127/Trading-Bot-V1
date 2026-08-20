"""Unit tests for the Upstox authentication helpers."""

from __future__ import annotations

import os
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse
from unittest import mock

from backend.broker import auth as auth_module


def test_build_authorization_url_uses_configured_values() -> None:
    """The authorization URL should include the client id and redirect URI."""
    with mock.patch.dict(
        os.environ,
        {
            "UPSTOX_CLIENT_ID": "demo-client",
            "UPSTOX_REDIRECT_URI": "https://example.com/callback",
        },
        clear=True,
    ):
        url = auth_module.build_authorization_url()

    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "api.upstox.com"
    assert parsed.path == "/v2/login/authorization/dialog"
    assert query["client_id"] == ["demo-client"]
    assert query["redirect_uri"] == ["https://example.com/callback"]


def test_exchange_code_for_token_posts_expected_payload() -> None:
    """The token exchange helper should post the expected payload to Upstox."""
    captured: Dict[str, Any] = {}

    def fake_post(url: str, payload: Dict[str, str], timeout: int) -> Dict[str, Any]:
        captured["url"] = url
        captured["payload"] = payload
        captured["timeout"] = timeout
        return {"access_token": "token-123", "token_type": "Bearer"}

    with mock.patch.dict(
        os.environ,
        {
            "UPSTOX_CLIENT_ID": "demo-client",
            "UPSTOX_CLIENT_SECRET": "demo-secret",
            "UPSTOX_REDIRECT_URI": "https://example.com/callback",
        },
        clear=True,
    ), mock.patch.object(auth_module, "_post_json", side_effect=fake_post):
        result = auth_module.exchange_code_for_token("auth-code")

    assert result["access_token"] == "token-123"
    assert captured["url"] == auth_module.TOKEN_URL
    assert captured["payload"]["code"] == "auth-code"
    assert captured["payload"]["client_id"] == "demo-client"
    assert captured["payload"]["client_secret"] == "demo-secret"
    assert captured["payload"]["redirect_uri"] == "https://example.com/callback"
    assert captured["payload"]["grant_type"] == "authorization_code"
    assert captured["timeout"] == 10
