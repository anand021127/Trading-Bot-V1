"""Optional control-plane authentication for state-changing endpoints.

PHASE 5 security hardening (item 29): start/stop/kill/reset/exit/settings/
token/backtest-cancel endpoints have had NO authentication — safe behind a
VPN/Nginx allowlist, unsafe on any internet-facing surface.

Policy (fail-safe, documented in the readiness report):
  * CONTROL_TOKEN unset (default, paper/local dev): dependency is a no-op —
    existing deployments keep working exactly as before.
  * CONTROL_TOKEN set: every protected endpoint requires the token via the
    `X-Control-Token` header or an `Authorization: Bearer <token>` header.
    Failure → 401. Timing-safe comparison.
  * The Upstox access token is NEVER accepted as the control token, and the
    control token is never logged or echoed back.
"""
from __future__ import annotations

import hmac
import logging
import os
from typing import Optional

from fastapi import Header, HTTPException, status

logger = logging.getLogger(__name__)

_CONTROL_TOKEN_ENV = "CONTROL_TOKEN"


def _control_token() -> str:
    return (os.getenv(_CONTROL_TOKEN_ENV) or "").strip()


def control_token_configured() -> bool:
    return bool(_control_token())


def require_control_token(
    x_control_token: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
) -> None:
    """FastAPI dependency guarding state-changing endpoints."""
    expected = _control_token()
    if not expected:
        return  # not configured — paper/local mode, no-op (documented)

    supplied: Optional[str] = None
    if x_control_token:
        supplied = x_control_token.strip()
    elif authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()

    # The Upstox token must never be usable as the control token.
    upstox = (os.getenv("UPSTOX_ACCESS_TOKEN") or "").strip()
    if supplied and upstox and hmac.compare_digest(supplied, upstox):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid control token.",
        )

    if not supplied or not hmac.compare_digest(supplied, expected):
        logger.warning("CONTROL_AUTH_REJECTED reason=%s", "missing" if not supplied else "mismatch")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid control token.",
        )


__all__ = ["require_control_token", "control_token_configured"]
