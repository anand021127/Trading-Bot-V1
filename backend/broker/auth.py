"""Helpers for authenticating with the Upstox API.

The module exposes small, testable helpers for building the authorization URL
and exchanging an OAuth code for an access token.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

AUTHORIZATION_URL = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"


def _get_required_env(name: str) -> str:
    """Return an environment variable value or raise a helpful error."""
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _post_json(url: str, payload: Dict[str, str], timeout: int = 10) -> Dict[str, Any]:
    """Send a JSON POST request.

    The implementation intentionally uses the standard library so the helpers can
    be imported in lightweight test environments without extra dependencies.
    """
    import urllib.request
    import json

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def build_authorization_url() -> str:
    """Build the Upstox OAuth authorization URL from environment variables."""
    client_id = _get_required_env("UPSTOX_CLIENT_ID")
    redirect_uri = _get_required_env("UPSTOX_REDIRECT_URI")

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
    }
    query_string = "&".join(f"{key}={value}" for key, value in params.items())
    return f"{AUTHORIZATION_URL}?{query_string}"


def exchange_code_for_token(code: str) -> Dict[str, Any]:
    """Exchange an authorization code for an Upstox access token."""
    payload = {
        "code": code,
        "client_id": _get_required_env("UPSTOX_CLIENT_ID"),
        "client_secret": _get_required_env("UPSTOX_CLIENT_SECRET"),
        "redirect_uri": _get_required_env("UPSTOX_REDIRECT_URI"),
        "grant_type": "authorization_code",
    }
    return _post_json(TOKEN_URL, payload, timeout=10)
