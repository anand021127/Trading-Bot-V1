"""Refuse order placement when the Upstox token is expired or missing."""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class TokenGuardError(RuntimeError):
    pass


def assert_token_usable(token: Optional[str], *, context: str = "order") -> None:
    if not token or not str(token).strip():
        logger.error("AUTH_FAILURE context=%s reason=missing_token", context)
        raise TokenGuardError("Upstox access token missing. Complete OAuth login.")
    try:
        from backend.broker.token_resolver import check_token_freshness
        info = check_token_freshness(token)
    except Exception as exc:
        logger.error("AUTH_FAILURE context=%s reason=inspect_failed err=%s", context, type(exc).__name__)
        raise TokenGuardError("Unable to inspect Upstox token") from exc
    if info.get("is_expired"):
        logger.error("AUTH_FAILURE context=%s reason=expired", context)
        raise TokenGuardError("Upstox access token expired. Re-run OAuth login.")
    # do not log the token
