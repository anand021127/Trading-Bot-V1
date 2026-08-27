"""Helpers for authenticating with the Upstox API.
The module exposes small, testable helpers for building the authorization URL
and exchanging an OAuth code for an access token using standard OAuth 2.0.
"""
from __future__ import annotations

import os
import urllib.parse
import urllib.request
import json
from typing import Any, Dict, Optional

AUTHORIZATION_URL = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
DEFAULT_REDIRECT_URI = "https://upstoxbot-anand.duckdns.org/api/settings/token-callback"

REQUEST_TOKEN_URL = "https://api.upstox.com/v3/login/auth/token/request"

# Aliases for explicit protocol compatibility
UPSTOX_AUTH_URL = AUTHORIZATION_URL
UPSTOX_TOKEN_URL = TOKEN_URL


def _get_required_env(name: str) -> str:
    """Return an environment variable value or fallback to database settings."""
    value = os.getenv(name)
    if value and not value.startswith("your_") and "your-api" not in value:
        return value

    try:
        from backend.database.db_manager import DatabaseManager
        db = DatabaseManager()
        setting_key = name.lower()
        db_val = db.get_setting(setting_key, "")
        if db_val and not db_val.startswith("your_"):
            return db_val
    except Exception:
        pass

    if name == "UPSTOX_REDIRECT_URI":
        return DEFAULT_REDIRECT_URI

    if not value or value.startswith("your_"):
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _get_redirect_uri(override: Optional[str] = None) -> str:
    """Resolve redirect URI with fallback to production DuckDNS endpoint."""
    if override and override.strip():
        return override.strip()
    env_uri = os.getenv("UPSTOX_REDIRECT_URI", "").strip()
    if env_uri and "your-api" not in env_uri and "dummy" not in env_uri:
        return env_uri
    try:
        from backend.database.db_manager import DatabaseManager
        db = DatabaseManager()
        db_uri = db.get_setting("upstox_redirect_uri", "")
        if db_uri and "your-api" not in db_uri and "dummy" not in db_uri:
            return db_uri
    except Exception:
        pass
    return DEFAULT_REDIRECT_URI


def _post_form(url: str, payload: Dict[str, str], timeout: int = 15) -> Dict[str, Any]:
    """Send a form-urlencoded POST request to Upstox API."""
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(url: str, payload: Dict[str, str], timeout: int = 10) -> Dict[str, Any]:
    """Send a JSON POST request."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def build_authorization_url(
    client_id: Optional[str] = None,
    redirect_uri: Optional[str] = None,
    state: Optional[str] = None,
) -> str:
    """Build the Upstox OAuth authorization URL from environment variables."""
    cid = client_id or _get_required_env("UPSTOX_CLIENT_ID")
    r_uri = _get_redirect_uri(redirect_uri)
    params = {
        "response_type": "code",
        "client_id": cid,
        "redirect_uri": r_uri,
    }
    if state:
        params["state"] = state
    query_string = urllib.parse.urlencode(params)
    return f"{AUTHORIZATION_URL}?{query_string}"


def exchange_code_for_token(
    code: str,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    redirect_uri: Optional[str] = None,
) -> Dict[str, Any]:
    """Exchange an authorization code for an Upstox access token using OAuth 2.0."""
    cid = client_id or _get_required_env("UPSTOX_CLIENT_ID")
    sec = client_secret or _get_required_env("UPSTOX_CLIENT_SECRET")
    r_uri = _get_redirect_uri(redirect_uri)
    payload = {
        "code": code,
        "client_id": cid,
        "client_secret": sec,
        "redirect_uri": r_uri,
        "grant_type": "authorization_code",
    }
    return _post_form(TOKEN_URL, payload, timeout=15)


def request_token_approval(client_id: Optional[str] = None, client_secret: Optional[str] = None) -> Dict[str, Any]:
    """Initiate Upstox API v3 semi-automated Access Token Request flow (preserved for backward compatibility)."""
    cid = client_id or _get_required_env("UPSTOX_CLIENT_ID")
    sec = client_secret or _get_required_env("UPSTOX_CLIENT_SECRET")
    url = f"{REQUEST_TOKEN_URL}/{cid}"
    payload = {
        "client_secret": sec,
    }
    return _post_json(url, payload, timeout=15)
