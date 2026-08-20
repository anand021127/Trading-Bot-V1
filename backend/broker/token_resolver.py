"""Authoritative Upstox Access Token Resolver.

Enforces strict token priority across the entire application:
1. Explicit runtime parameter
2. SQLite Database token (persisted via OAuth callback)
3. Environment variable (fallback only if database is empty)
"""
from __future__ import annotations

import os
import hashlib
from typing import Any, Dict, Optional
from backend.database.db_manager import DatabaseManager


def resolve_upstox_token(explicit_token: Optional[str] = None) -> str:
    """Resolve Upstox access token with strict priority:
    1. Explicit runtime token
    2. SQLite database token (primary source after OAuth)
    3. Environment variable fallback
    """
    if explicit_token and explicit_token.strip():
        return explicit_token.strip()

    # 1. Check SQLite database
    try:
        db = DatabaseManager()
        db_token = db.load_token()
        if db_token and db_token.strip():
            return db_token.strip()
    except Exception:
        pass

    # 2. Check environment variable as fallback
    env_token = os.getenv("UPSTOX_ACCESS_TOKEN", "")
    if env_token and env_token.strip():
        return env_token.strip()

    return ""


def get_token_source(explicit_token: Optional[str] = None) -> str:
    """Determine the origin source of the active token."""
    if explicit_token and explicit_token.strip():
        return "runtime"

    try:
        db = DatabaseManager()
        db_token = db.load_token()
        if db_token and db_token.strip():
            return "database"
    except Exception:
        pass

    if os.getenv("UPSTOX_ACCESS_TOKEN", "").strip():
        return "environment"

    return "none"


def get_token_metadata(explicit_token: Optional[str] = None) -> Dict[str, Any]:
    """Return safe metadata (fingerprint, length, source) without exposing secrets."""
    token = resolve_upstox_token(explicit_token)
    source = get_token_source(explicit_token)

    if not token:
        return {
            "present": False,
            "length": 0,
            "fingerprint": "NONE",
            "source": "none",
        }

    sha = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return {
        "present": True,
        "length": len(token),
        "fingerprint": f"{sha[:6]}...{sha[-6:]}",
        "source": source,
    }
