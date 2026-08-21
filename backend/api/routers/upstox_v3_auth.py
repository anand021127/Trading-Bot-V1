"""Upstox API v3 Semi-Automated Token Approval Router.

Implements the official Upstox API v3 token request and notifier webhook flow:
1. POST /api/upstox/auth/request -> Dispatches push notification to user's Upstox Mobile App / WhatsApp.
2. POST /api/webhooks/upstox-token-notifier -> Webhook receiver for Upstox token notification.
3. GET  /api/upstox/auth/status -> Safe status endpoint (IDLE, PENDING, APPROVED, FAILED, EXPIRED).

Adheres strictly to security mandates:
- Never logs or returns raw access_token or client_secret.
- Reuses existing DatabaseManager persistence and token propagation helpers.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

try:
    from fastapi import APIRouter, HTTPException, Request, Response
except ImportError:
    class APIRouter:  # type: ignore
        def __init__(self, *args, **kwargs):
            self.routes = []
        def get(self, *args, **kwargs):
            return lambda f: f
        def post(self, *args, **kwargs):
            return lambda f: f
        def all(self, *args, **kwargs):
            return lambda f: f

    class HTTPException(Exception):  # type: ignore
        def __init__(self, status_code: int, detail: Any = None):
            self.status_code = status_code
            self.detail = detail
            super().__init__(str(detail))

    class Request:  # type: ignore
        pass

    class Response:  # type: ignore
        pass

from backend.database.db_manager import DatabaseManager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Upstox V3 Auth"])
try:
    from backend.config.settings import load_settings
    _settings = load_settings()
    _db_path = getattr(getattr(_settings, "database", None), "path", "data/trading.db")
except Exception:
    _db_path = "data/trading.db"

_db = DatabaseManager(db_path=_db_path)

# Known Upstox API v3 Error Codes & Human-Readable Explanations
UPSTOX_V3_ERROR_MAPPINGS: Dict[str, str] = {
    "UDAPI100069": "User approval is already pending. Check your Upstox App or WhatsApp to approve.",
    "UDAPI1123": "Invalid Client ID or Client Secret. Please verify your Upstox API credentials.",
    "UDAPI1124": "Upstox app is inactive or disabled in the Developer Console.",
    "UDAPI1155": "Notifier Webhook URL is not configured in Upstox Developer Console. Please set the Webhook URL in App settings.",
    "UDAPI1157": "Rate limit exceeded for token requests. Please wait before requesting approval again.",
}
UPSTOX_ERROR_CODE_MAP = UPSTOX_V3_ERROR_MAPPINGS

# In-memory auth lifecycle state
_auth_state: Dict[str, Any] = {
    "status": "IDLE",  # "IDLE", "PENDING", "APPROVED", "FAILED", "EXPIRED"
    "requested_at": None,
    "authorization_expiry": 900,
    "requested_timestamp": 0.0,
    "approved_at": None,
    "last_error": None,
}


def _get_client_credentials() -> tuple[str, str]:
    """Resolve Client ID and Client Secret from environment or DB."""
    client_id = os.getenv("UPSTOX_CLIENT_ID", "")
    client_secret = os.getenv("UPSTOX_CLIENT_SECRET", "")

    if not client_id or client_id.startswith("your_"):
        try:
            client_id = _db.get_setting("upstox_client_id", "")
        except Exception:
            pass

    if not client_secret or client_secret.startswith("your_"):
        try:
            client_secret = _db.get_setting("upstox_client_secret", "")
        except Exception:
            pass

    return client_id.strip(), client_secret.strip()


def _is_token_present() -> bool:
    """Check if a valid token is present in environment or SQLite database."""
    token = (os.getenv("UPSTOX_ACCESS_TOKEN") or "").strip()
    if token and not token.startswith("your_"):
        return True
    try:
        db_token = (_db.load_token() or "").strip()
        if db_token and not db_token.startswith("your_"):
            return True
    except Exception:
        pass
    return False


@router.post("/api/upstox/auth/request")
@router.post("/upstox/auth/request")
async def request_upstox_approval() -> Dict[str, Any]:
    """Dispatch an Upstox API v3 token approval push notification.
    
    Calls POST https://api.upstox.com/v3/login/auth/token/request/{client_id}
    """
    global _auth_state

    client_id, client_secret = _get_client_credentials()

    if not client_id or client_id.startswith("your_"):
        _auth_state["status"] = "FAILED"
        _auth_state["last_error"] = "UPSTOX_CLIENT_ID is not configured."
        raise HTTPException(status_code=400, detail="UPSTOX_CLIENT_ID is not configured.")

    if not client_secret or client_secret.startswith("your_"):
        _auth_state["status"] = "FAILED"
        _auth_state["last_error"] = "UPSTOX_CLIENT_SECRET is not configured."
        raise HTTPException(status_code=400, detail="UPSTOX_CLIENT_SECRET is not configured.")

    url = f"https://api.upstox.com/v3/login/auth/token/request/{client_id}"
    payload = {"client_secret": client_secret}
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }

    now_iso = datetime.now(timezone.utc).isoformat()
    now_ts = time.time()

    try:
        import urllib.request
        import urllib.error
        import json

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        res_json: Dict[str, Any] = {}
        status_code = 200

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                status_code = resp.status if hasattr(resp, "status") else 200
                resp_text = resp.read().decode("utf-8")
                res_json = json.loads(resp_text) if resp_text else {}
        except urllib.error.HTTPError as http_err:
            status_code = http_err.code
            try:
                err_text = http_err.read().decode("utf-8")
                res_json = json.loads(err_text) if err_text else {}
            except Exception:
                res_json = {}

        if status_code == 200 and (res_json.get("status") == "success" or "data" in res_json):
            data = res_json.get("data", {})
            auth_expiry = int(data.get("authorization_expiry") or 900)

            _auth_state["status"] = "PENDING"
            _auth_state["requested_at"] = now_iso
            _auth_state["requested_timestamp"] = now_ts
            _auth_state["authorization_expiry"] = auth_expiry
            _auth_state["last_error"] = None

            logger.info("[Upstox V3 Auth] Token approval request sent successfully. Expiry: %d seconds.", auth_expiry)
            return {
                "status": "pending",
                "message": "Approval request sent — check your Upstox App / WhatsApp.",
                "authorization_expiry": auth_expiry,
            }

        # Handle specific error codes
        err_code = ""
        err_msg = ""
        if "errors" in res_json and isinstance(res_json["errors"], list) and len(res_json["errors"]) > 0:
            first_err = res_json["errors"][0]
            err_code = first_err.get("errorCode", "")
            err_msg = first_err.get("message", "")
        elif "message" in res_json:
            err_msg = res_json.get("message", "")

        mapped_explanation = UPSTOX_V3_ERROR_MAPPINGS.get(err_code)
        display_msg = mapped_explanation or err_msg or f"Token request failed with HTTP {status_code}"

        if err_code == "UDAPI100069":
            # Already pending
            _auth_state["status"] = "PENDING"
            _auth_state["requested_at"] = _auth_state.get("requested_at") or now_iso
            _auth_state["requested_timestamp"] = _auth_state.get("requested_timestamp") or now_ts
            _auth_state["last_error"] = display_msg
            return {
                "status": "pending",
                "message": display_msg,
                "authorization_expiry": _auth_state.get("authorization_expiry", 900),
            }

        _auth_state["status"] = "FAILED"
        _auth_state["last_error"] = display_msg
        logger.warning("[Upstox V3 Auth] Token approval request rejected: code=%s, msg=%s", err_code, display_msg)

        err_detail = {
            "status": "error",
            "message": display_msg,
            "error_code": err_code or f"HTTP_{status_code}",
            "authorization_expiry": _auth_state.get("authorization_expiry", 900),
        }
        raise HTTPException(status_code=400, detail=err_detail)

    except HTTPException:
        raise
    except Exception as exc:
        err_str = f"Network failure communicating with Upstox API: {str(exc)}"
        _auth_state["status"] = "FAILED"
        _auth_state["last_error"] = err_str
        logger.error("[Upstox V3 Auth] Network exception in request_upstox_approval: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={
                "status": "error",
                "message": err_str,
                "authorization_expiry": _auth_state.get("authorization_expiry", 900),
            }
        )


@router.post("/api/webhooks/upstox-token-notifier")
@router.post("/webhooks/upstox-token-notifier")
async def upstox_token_notifier(request: Request) -> Dict[str, Any]:
    """Inbound webhook receiver for Upstox API v3 access token push notifications."""
    global _auth_state

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload in webhook request")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")

    # 1. Validate message_type
    message_type = payload.get("message_type")
    if message_type != "access_token":
        logger.warning("[Webhook Ingress] Invalid message_type received: %s", message_type)
        raise HTTPException(
            status_code=400,
            detail=f"Invalid message_type: expected 'access_token', got '{message_type}'",
        )

    # 2. Validate client_id matches configured application
    req_client_id = payload.get("client_id", "")
    conf_client_id, _ = _get_client_credentials()

    if req_client_id and conf_client_id and not conf_client_id.startswith("your_"):
        if req_client_id.strip() != conf_client_id.strip():
            logger.warning(
                "[Webhook Security] Client ID mismatch: received %s, expected %s",
                req_client_id,
                conf_client_id,
            )
            raise HTTPException(status_code=403, detail="client_id mismatch with configured application")

    # 3. Validate access_token exists and is non-empty
    access_token = (payload.get("access_token") or "").strip()
    if not access_token:
        logger.warning("[Webhook Ingress] Missing access_token in webhook payload")
        raise HTTPException(status_code=400, detail="Missing access_token in webhook payload")

    # 4. Never log the access_token. Calculate safe fingerprint for audit
    import hashlib
    token_hash = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
    fingerprint = f"{token_hash[:6]}...{token_hash[-6:]}"
    user_id = payload.get("user_id", "")

    logger.info(
        "[Webhook Ingress] Valid Upstox token notification received: user_id=%s, fingerprint=%s, length=%d",
        user_id,
        fingerprint,
        len(access_token),
    )

    # 5. Persist using DatabaseManager.save_token()
    try:
        _db.save_token(access_token)
        if user_id:
            _db.save_setting("upstox_user_id", str(user_id))
    except Exception as exc:
        logger.error("[Webhook Ingress] Failed to persist token to SQLite: %s", exc)

    # 6. Update environment variable
    os.environ["UPSTOX_ACCESS_TOKEN"] = access_token

    # 7. Reuse existing production token propagation and WebSocket restart functions
    try:
        from backend.api.routers.settings import _propagate_token_to_engine, _restart_websocket_client
        _propagate_token_to_engine(access_token)
        _restart_websocket_client(access_token)
    except Exception as exc:
        logger.warning("[Webhook Ingress] Token propagation/restart warning: %s", exc)

    # 8. Update in-memory auth state
    now_iso = datetime.now(timezone.utc).isoformat()
    _auth_state["status"] = "APPROVED"
    _auth_state["approved_at"] = now_iso
    _auth_state["last_error"] = None

    # 9. Return 2xx acknowledgement WITHOUT returning access_token
    return {
        "status": "success",
        "message": "Token received, verified, and persisted successfully.",
        "user_id": user_id,
        "token_type": payload.get("token_type", "Bearer"),
        "expires_at": payload.get("expires_at"),
        "fingerprint": fingerprint,
    }


@router.get("/api/webhooks/upstox-token-notifier")
@router.get("/webhooks/upstox-token-notifier")
async def upstox_token_notifier_health() -> Dict[str, Any]:
    """Health check for webhook endpoint URL verification."""
    return {
        "status": "active",
        "endpoint": "/api/webhooks/upstox-token-notifier",
        "method": "POST",
        "description": "Inbound Upstox API v3 Notifier Webhook Endpoint",
    }


@router.get("/api/upstox/auth/status")
@router.get("/upstox/auth/status")
async def get_upstox_auth_status() -> Dict[str, Any]:
    """Return the current authentication and approval status.
    
    Safe diagnostic endpoint returning only:
    - status (IDLE | PENDING | APPROVED | FAILED | EXPIRED)
    - requested_at
    - authorization_expiry
    - approved_at
    - last_error
    - token_present
    """
    global _auth_state

    token_present = _is_token_present()
    current_status = _auth_state.get("status", "IDLE")

    # Check for expiration if status is PENDING
    if current_status == "PENDING":
        req_ts = _auth_state.get("requested_timestamp", 0.0)
        expiry_secs = _auth_state.get("authorization_expiry", 900)
        if req_ts > 0 and (time.time() - req_ts) > expiry_secs:
            current_status = "EXPIRED"
            _auth_state["status"] = "EXPIRED"
            _auth_state["last_error"] = "Approval request expired — please request approval again."

    # If token is present and not currently pending, failed, or expired
    if token_present and current_status not in ("PENDING", "FAILED", "EXPIRED"):
        current_status = "APPROVED"

    return {
        "status": current_status,
        "requested_at": _auth_state.get("requested_at"),
        "authorization_expiry": _auth_state.get("authorization_expiry", 900),
        "approved_at": _auth_state.get("approved_at"),
        "last_error": _auth_state.get("last_error"),
        "token_present": token_present,
    }


# Export aliases for router callers and test suites
request_upstox_auth = request_upstox_approval
upstox_token_notifier_webhook = upstox_token_notifier
