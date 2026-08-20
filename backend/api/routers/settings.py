"""Settings router — persistent configuration via SQLite + YAML fallback."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from fastapi import APIRouter, HTTPException, Request

from backend.config.settings import load_settings
from backend.database.db_manager import DatabaseManager

router = APIRouter()
settings = load_settings()

SETTINGS_PATH = Path(__file__).resolve().parents[3] / "backend" / "config" / "settings.yaml"
_db = DatabaseManager(db_path=settings.database.path)


def _yaml_defaults() -> Dict[str, Any]:
    """Load defaults from settings.yaml (always available)."""
    return {
        "mode": settings.mode,
        "broker_base_url": settings.broker.base_url,
        "frontend_url": os.getenv("FRONTEND_URL", ""),
        "capital": {
            "total": settings.capital.total,
            "max_allocation_per_trade": settings.capital.max_allocation_per_trade,
            "cash_buffer": settings.capital.cash_buffer,
        },
        "risk": {
            "max_risk_per_trade_pct":   settings.risk.max_risk_per_trade_pct,
            "max_daily_loss_pct":       settings.risk.max_daily_loss_pct,
            "max_trades_per_day":       settings.risk.max_trades_per_day,
            "max_concurrent_positions": settings.risk.max_concurrent_positions,
            "max_consecutive_losses":   settings.risk.max_consecutive_losses,
        },
        "strategy": {
            "orb_window_start":   getattr(settings.strategy, "orb_window_start",   "09:15"),
            "orb_window_end":     getattr(settings.strategy, "orb_window_end",     "09:30"),
            "entry_window_start": getattr(settings.strategy, "entry_window_start", "09:30"),
            "entry_window_end":   getattr(settings.strategy, "entry_window_end",   "12:30"),
            "exit_all_by":        getattr(settings.strategy, "exit_all_by",        "14:45"),
        },
        "indicators": {
            "ema_fast":          getattr(settings.indicators, "ema_fast",          20),
            "ema_slow":          getattr(settings.indicators, "ema_slow",          50),
            "ema_trend":         getattr(settings.indicators, "ema_trend",         200),
            "rsi_period":        getattr(settings.indicators, "rsi_period",        14),
            "rsi_min":           getattr(settings.indicators, "rsi_min",           55),
            "rsi_max":           getattr(settings.indicators, "rsi_max",           75),
            "atr_period":        getattr(settings.indicators, "atr_period",        14),
            "choppiness_max":    getattr(settings.indicators, "choppiness_max",    61.8),
            "volume_multiplier": getattr(settings.indicators, "volume_multiplier", 1.5),
        },
        "notifications": {
            "email_enabled":    getattr(settings.notifications, "email_enabled",    False),
            "telegram_enabled": getattr(settings.notifications, "telegram_enabled", False),
            "sender_email":     bool(os.getenv("SENDER_EMAIL") or os.getenv("NOTIFICATION_EMAIL") or getattr(settings.notifications, "sender_email", "")),
            "recipient_email":  bool(os.getenv("RECIPIENT_EMAIL") or os.getenv("NOTIFICATION_EMAIL") or getattr(settings.notifications, "recipient_email", "")),
        },
    }


@router.get("/")
async def get_settings() -> Dict[str, Any]:
    """
    Return settings. Priority: DB (persistent) > YAML defaults.
    This means saved settings survive Render restarts.
    """
    try:
        blob = _db.load_settings_blob()
        if blob:
            return blob
    except Exception:
        pass
    return _yaml_defaults()


@router.put("/")
async def update_settings(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Persist settings to SQLite DB (survives restarts) AND settings.yaml (best-effort).
    """
    try:
        # Load existing saved settings (or defaults)
        current: Dict[str, Any] = {}
        try:
            current = _db.load_settings_blob() or _yaml_defaults()
        except Exception:
            current = _yaml_defaults()

        # Deep-merge only known keys
        for key in ("mode", "capital", "risk", "strategy", "indicators", "notifications", "universe"):
            if key in body:
                if isinstance(body[key], dict) and isinstance(current.get(key), dict):
                    current[key].update(body[key])
                else:
                    current[key] = body[key]

        # Save to SQLite (primary persistent storage)
        _db.save_settings_blob(current)

        # Also try to update settings.yaml (best-effort, may be read-only on Render)
        try:
            if SETTINGS_PATH.exists():
                with open(SETTINGS_PATH) as f:
                    yaml_current = yaml.safe_load(f) or {}
                for key in ("mode", "capital", "risk", "strategy", "indicators", "notifications"):
                    if key in current:
                        if isinstance(current[key], dict) and isinstance(yaml_current.get(key), dict):
                            yaml_current[key].update(current[key])
                        else:
                            yaml_current[key] = current[key]
                with open(SETTINGS_PATH, "w") as f:
                    yaml.dump(yaml_current, f, default_flow_style=False, allow_unicode=True)
        except Exception:
            pass  # Read-only filesystem on Render is fine — DB is the source of truth

        return {"saved": True, "restart_required": False, "storage": "database"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/env-status")
async def get_env_status() -> Dict[str, bool]:
    """Return which env vars are set (never expose values)."""
    # Also check DB-stored token
    db_token = ""
    try:
        db_token = _db.load_token()
    except Exception:
        pass
    return {
        "UPSTOX_CLIENT_ID":     bool(os.getenv("UPSTOX_CLIENT_ID")),
        "UPSTOX_CLIENT_SECRET": bool(os.getenv("UPSTOX_CLIENT_SECRET")),
        "UPSTOX_ACCESS_TOKEN":  bool(os.getenv("UPSTOX_ACCESS_TOKEN") or db_token),
        "EMAIL_PASSWORD":       bool(os.getenv("EMAIL_PASSWORD")),
        "SENDER_EMAIL":         bool(os.getenv("SENDER_EMAIL") or os.getenv("NOTIFICATION_EMAIL")),
        "RECIPIENT_EMAIL":      bool(os.getenv("RECIPIENT_EMAIL") or os.getenv("NOTIFICATION_EMAIL")),
        "TELEGRAM_BOT_TOKEN":   bool(os.getenv("TELEGRAM_BOT_TOKEN")),
        "TELEGRAM_CHAT_ID":     bool(os.getenv("TELEGRAM_CHAT_ID")),
    }


DEFAULT_REDIRECT_URI = "https://upstoxbot-anand.duckdns.org/api/settings/token-callback"


@router.get("/auth-url")
@router.get("/login-url")
@router.post("/login-url")
@router.get("/regenerate-token")
@router.post("/regenerate-token")
async def regenerate_token(
    request: Request,
    client_id: Optional[str] = None,
    redirect_uri: Optional[str] = None,
) -> Dict[str, str]:
    """Return Upstox OAuth authorization URL."""
    body_data: Dict[str, Any] = {}
    if request.method == "POST":
        try:
            body_data = await request.json()
        except Exception:
            pass

    cid = client_id or body_data.get("client_id") or os.getenv("UPSTOX_CLIENT_ID", "")
    if not cid or cid.startswith("your_client_id"):
        try:
            db_cid = _db.get_setting("upstox_client_id", "")
            if db_cid and not db_cid.startswith("your_client_id"):
                cid = db_cid
        except Exception:
            pass

    env_redirect = os.getenv("UPSTOX_REDIRECT_URI", "")
    r_uri = (
        redirect_uri
        or body_data.get("redirect_uri")
        or (env_redirect if (env_redirect and "your-api" not in env_redirect and "dummy" not in env_redirect) else DEFAULT_REDIRECT_URI)
    )

    if not cid or cid.startswith("your_client_id"):
        raise HTTPException(status_code=400, detail="UPSTOX_CLIENT_ID not configured.")

    import urllib.parse
    params = {
        "response_type": "code",
        "client_id": cid,
        "redirect_uri": r_uri,
    }
    auth_url = f"https://api.upstox.com/v2/login/authorization/dialog?{urllib.parse.urlencode(params)}"
    return {"auth_url": auth_url, "redirect_uri": r_uri}


@router.get("/token-callback")
@router.post("/token-callback")
async def token_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
) -> Any:
    """Handle OAuth redirect from Upstox — exchange authorization code for access token,
    gate strictly on HTTP 200 /v2/user/profile, and persist to SQLite and runtime.
    """
    from fastapi.responses import HTMLResponse
    from backend.broker.token_resolver import resolve_upstox_token, get_token_metadata
    from backend.broker.upstox_client import UpstoxClient

    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost:8000"
    env_redirect = os.getenv("UPSTOX_REDIRECT_URI", "")
    redirect_uri = env_redirect if (env_redirect and "your-api" not in env_redirect and "dummy" not in env_redirect) else DEFAULT_REDIRECT_URI

    # Safe callback diagnostics (NO secrets logged)
    logger.info(
        "[OAuth Callback Diagnostics] Received callback: timestamp=%s, server_identity=FastAPI(pid=%s), host=%s, origin=%s, configured_redirect_uri=%s, has_code=%s, has_state=%s, has_error=%s",
        datetime.now(timezone.utc).isoformat(),
        os.getpid(),
        host,
        request.headers.get("origin") or request.headers.get("referer") or "none",
        redirect_uri,
        bool(code),
        bool(state),
        bool(error),
    )

    if error or error_description:
        err_msg = error_description or error or "OAuth authorization was denied or encountered an error."
        html_err = f"""<!DOCTYPE html><html><body style="font-family:sans-serif;background:#0b0f19;color:#ef4444;padding:40px;text-align:center;">
        <h2>OAuth Authentication Error</h2>
        <p style="color:#94a3b8;">{err_msg}</p>
        <button onclick="window.close()" style="background:#1e293b;color:#fff;border:none;padding:8px 16px;border-radius:6px;cursor:pointer;">Close Window</button>
        </body></html>"""
        return HTMLResponse(content=html_err, status_code=400)

    if not code:
        html_err = """<!DOCTYPE html><html><body style="font-family:sans-serif;background:#0b0f19;color:#ef4444;padding:40px;text-align:center;">
        <h2>OAuth Authentication Failed</h2>
        <p>No authorization code received in redirect parameters.</p>
        <button onclick="window.close()" style="background:#1e293b;color:#fff;border:none;padding:8px 16px;border-radius:6px;cursor:pointer;">Close Window</button>
        </body></html>"""
        return HTMLResponse(content=html_err, status_code=400)

    try:
        import httpx
        client_id     = os.getenv("UPSTOX_CLIENT_ID", "")
        client_secret = os.getenv("UPSTOX_CLIENT_SECRET", "")

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.upstox.com/v2/login/authorization/token",
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            )
        data = r.json()
        token = data.get("access_token", "")

        if token:
            # Mandatory Step: Verify against Upstox /v2/user/profile BEFORE persistence (MUST be HTTP 200)
            upstox_client = UpstoxClient(access_token=token)
            profile_data = upstox_client.get_profile()

            if profile_data and profile_data.get("status") == "success":
                # Only on HTTP 200: Persist to SQLite, update runtime and propagate
                try:
                    _db.save_token(token)
                    user_name = profile_data.get("data", {}).get("user_name") or profile_data.get("data", {}).get("user_id") or "Upstox Trader"
                    user_id = profile_data.get("data", {}).get("user_id", "")
                    if user_name:
                        _db.save_setting("upstox_user_name", user_name)
                    if user_id:
                        _db.save_setting("upstox_user_id", user_id)
                except Exception as e:
                    logger.error("Failed to persist token to SQLite: %s", e)

                os.environ["UPSTOX_ACCESS_TOKEN"] = token
                _propagate_token_to_engine(token)
                _restart_websocket_client(token)

                user_name = profile_data.get("data", {}).get("user_name") or profile_data.get("data", {}).get("user_id") or "Upstox Trader"

                html_ok = f"""<!DOCTYPE html><html><body style="font-family:sans-serif;background:#0b0f19;color:#10b981;padding:40px;text-align:center;">
                <h2>Upstox Authentication Successful</h2>
                <p style="color:#e2e8f0;">Welcome, <strong>{user_name}</strong>!</p>
                <p style="color:#94a3b8;font-size:14px;">Token verified against Upstox Profile API (HTTP 200) and persisted to SQLite.</p>
                <p style="color:#64748b;font-size:12px;">This window will close automatically.</p>
                <script>
                  if (window.opener) {{
                    window.opener.postMessage({{ type: 'UPSTOX_AUTH_SUCCESS', user_name: '{user_name}' }}, '*');
                  }}
                  setTimeout(() => window.close(), 1500);
                </script>
                </body></html>"""
                return HTMLResponse(content=html_ok, status_code=200)
            else:
                # Do NOT persist if profile verification fails
                html_invalid = """<!DOCTYPE html><html><body style="font-family:sans-serif;background:#0b0f19;color:#ef4444;padding:40px;text-align:center;">
                <h2>Authentication Verification Failed</h2>
                <p style="color:#94a3b8;">Token exchange succeeded but Upstox profile verification failed. Token was NOT persisted.</p>
                <button onclick="window.close()" style="background:#1e293b;color:#fff;border:none;padding:8px 16px;border-radius:6px;cursor:pointer;">Close Window</button>
                </body></html>"""
                return HTMLResponse(content=html_invalid, status_code=401)

        err_msg = data.get("message") or "Token exchange failed."
        html_fail = f"""<!DOCTYPE html><html><body style="font-family:sans-serif;background:#0b0f19;color:#ef4444;padding:40px;text-align:center;">
        <h2>Token Exchange Failed</h2>
        <p style="color:#94a3b8;">{err_msg}</p>
        <button onclick="window.close()" style="background:#1e293b;color:#fff;border:none;padding:8px 16px;border-radius:6px;cursor:pointer;">Close Window</button>
        </body></html>"""
        return HTMLResponse(content=html_fail, status_code=400)
    except Exception as e:
        html_ex = f"""<!DOCTYPE html><html><body style="font-family:sans-serif;background:#0b0f19;color:#ef4444;padding:40px;text-align:center;">
        <h2>Error during Token Exchange</h2>
        <p style="color:#94a3b8;">{e}</p>
        <button onclick="window.close()" style="background:#1e293b;color:#fff;border:none;padding:8px 16px;border-radius:6px;cursor:pointer;">Close Window</button>
        </body></html>"""
        return HTMLResponse(content=html_ex, status_code=500)


def _propagate_token_to_engine(token: str) -> None:
    """Propagate fresh OAuth access token to the running TradingEngine and OrderManager."""
    try:
        from backend.api.routers.bot_control import get_engine
        engine = get_engine()
    except Exception:
        engine = None

    if engine is None:
        try:
            import backend.api.main as main_mod
            app = getattr(main_mod, "app", None)
            if app is not None:
                engine = getattr(app.state, "engine", None)
        except Exception:
            engine = None

    if engine is not None:
        try:
            if hasattr(engine, "update_access_token"):
                engine.update_access_token(token)
            else:
                if hasattr(engine, "client") and engine.client is not None:
                    engine.client.access_token = token
                if hasattr(engine, "order_manager") and getattr(engine.order_manager, "client", None) is not None:
                    engine.order_manager.client.access_token = token
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Could not propagate token to trading engine: %s", e)


def _restart_websocket_client(token: str) -> None:
    """Fix for a real bug: the v3 WebSocket client only started once at
    server boot with whatever token existed at that moment. If the token
    was saved to the DB AFTER boot (e.g. right now, via this OAuth
    callback), the socket stayed permanently in 'auth_failed' — REST calls
    worked because they re-check the token every request, but the socket
    never got the memo. This restarts it with the token that's actually
    valid right now."""
    try:
        import backend.api.main as main_mod
        from backend.broker.websocket_client import UpstoxWebSocketClient
        from backend.broker.upstox_client import ALL_INSTRUMENTS
        from backend.api.websocket import update_price_cache

        app = getattr(main_mod, "app", None)
        if app is None:
            return
        old_client = getattr(app.state, "ws_client", None)
        if old_client is not None:
            try:
                old_client.stop()
            except Exception:
                pass

        new_client = UpstoxWebSocketClient(
            access_token=token,
            instrument_keys=list(ALL_INSTRUMENTS.values()),
            on_price_update=update_price_cache,
            mode="full",
        )
        new_client.start()
        app.state.ws_client = new_client
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Could not restart WebSocket client with new token: %s", e)


@router.post("/disconnect-token")
async def disconnect_token() -> Dict[str, Any]:
    """Fully clear the Upstox token from both the environment and the
    persisted DB, and stop the WebSocket client. Use this to get back to a
    genuinely clean/disconnected state — without it, a token saved in an
    earlier session keeps getting silently reloaded on every request."""
    os.environ["UPSTOX_ACCESS_TOKEN"] = ""
    try:
        _db.save_token("")
    except Exception:
        pass

    try:
        import backend.api.main as main_mod
        app = getattr(main_mod, "app", None)
        client = getattr(app.state, "ws_client", None) if app else None
        if client is not None:
            client.stop()
            app.state.ws_client = None
    except Exception:
        pass

    return {"status": "disconnected", "message": "Token cleared from DB and environment. WebSocket stopped."}


@router.get("/broker-status")
async def get_broker_status() -> Dict[str, Any]:
    """
    Real broker connection status check.
    Does NOT just check if token exists — actually calls Upstox API.
    """
    from backend.broker.token_resolver import resolve_upstox_token, get_token_metadata
    meta = get_token_metadata()
    token = resolve_upstox_token()

    status = {
        "token_present": meta["present"],
        "token_source": meta["source"],
        "token_fingerprint": meta["fingerprint"],
        "token_valid": False,
        "api_reachable": False,
        "websocket_url": settings.broker.websocket_url,
        "overall": "DISCONNECTED",
        "reason": "",
    }

    if not token:
        status["reason"] = "No access token found. Go to Settings → Generate Token."
        return status

    try:
        from backend.broker.upstox_client import UpstoxClient
        client = UpstoxClient(access_token=token)
        valid = client.is_token_valid()
        status["token_valid"] = valid
        status["api_reachable"] = True

        if valid:
            status["overall"] = "CONNECTED"
            status["reason"] = "Token valid and Upstox API reachable."
        else:
            status["overall"] = "AUTHENTICATION_FAILED"
            status["reason"] = "Token present but rejected by Upstox (401). Regenerate token in Settings."
    except Exception as e:
        err = str(e)
        status["overall"] = "AUTHENTICATION_FAILED" if "401" in err else "DISCONNECTED"
        status["reason"] = f"Upstox connection error: {err}"

    return status


@router.post("/save-token")
async def save_token_endpoint(body: Dict[str, Any]) -> Dict[str, Any]:
    """Manually or programmatically save token, verify against profile, and persist."""
    from backend.broker.upstox_client import UpstoxClient
    import hashlib

    token = (body.get("access_token") or body.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="No access token provided")

    client = UpstoxClient(access_token=token)
    profile = client.get_profile()

    if profile:
        _db.save_token(token)
        os.environ["UPSTOX_ACCESS_TOKEN"] = token
        _propagate_token_to_engine(token)
        _restart_websocket_client(token)
        sha = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return {
            "saved": True,
            "verified": True,
            "http_status": 200,
            "token_fingerprint": f"{sha[:6]}...{sha[-6:]}",
            "token_length": len(token),
            "user_name": profile.get("user_name", "Upstox Trader"),
            "user_id": profile.get("user_id", ""),
            "message": "Token verified and saved to database.",
        }
    else:
        raise HTTPException(
            status_code=400,
            detail="Token verification failed against Upstox /v2/user/profile."
        )


@router.get("/token-status")
async def token_status_endpoint() -> Dict[str, Any]:
    """Safe token status diagnostic."""
    from backend.broker.token_resolver import resolve_upstox_token, get_token_metadata
    from backend.broker.upstox_client import UpstoxClient

    token = resolve_upstox_token()
    meta = get_token_metadata()

    result = {
        "token_present": meta["present"],
        "token_source": meta["source"],
        "fingerprint": meta["fingerprint"],
        "length": meta["length"],
        "persisted": meta["source"] in ("database", "runtime"),
        "broker_verified": False,
        "profile_status": None,
    }

    if token:
        try:
            client = UpstoxClient(access_token=token)
            valid = client.is_token_valid()
            result["broker_verified"] = valid
            result["profile_status"] = 200 if valid else 401
        except Exception:
            result["broker_verified"] = False
            result["profile_status"] = None

    return result


@router.post("/request-token-push")
async def request_token_push_endpoint(request: Request) -> Dict[str, Any]:
    """Initiate Upstox API v3 semi-automated Access Token Request flow."""
    from backend.broker.auth import request_token_approval

    client_id = os.getenv("UPSTOX_CLIENT_ID", "")
    client_secret = os.getenv("UPSTOX_CLIENT_SECRET", "")

    if not client_id or client_id.startswith("your_client_id"):
        raise HTTPException(status_code=400, detail="UPSTOX_CLIENT_ID is not configured.")
    if not client_secret or client_secret.startswith("your_client_secret"):
        raise HTTPException(status_code=400, detail="UPSTOX_CLIENT_SECRET is not configured.")

    try:
        res = request_token_approval(client_id=client_id, client_secret=client_secret)
        status_val = res.get("status", "success")
        data_val = res.get("data", {})
        return {
            "status": "success" if status_val == "success" else "error",
            "message": "Token approval request sent to your Upstox mobile app / WhatsApp.",
            "approval_state": "WAITING_FOR_USER_APPROVAL",
            "authorization_expiry": data_val.get("authorization_expiry", 900),
            "expires_in_seconds": data_val.get("authorization_expiry", 900),
            "notifier_url": data_val.get("notifier_url"),
        }
    except Exception as e:
        logger.error("Failed to dispatch Upstox API v3 token request: %s", e)
        return {
            "status": "error",
            "message": f"Failed to initiate token request: {str(e)}",
            "approval_state": "REQUEST_FAILED",
            "error_code": "REQUEST_FAILED",
        }


@router.post("/webhooks/upstox-token-notifier")
@router.post("/upstox-token-notifier")
async def upstox_token_notifier_webhook(request: Request) -> Dict[str, Any]:
    """Inbound webhook receiver for Upstox API v3 token push approvals."""
    import hashlib
    from backend.broker.upstox_client import UpstoxClient
    from backend.broker.token_resolver import resolve_upstox_token

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")

    req_client_id = payload.get("client_id")
    conf_client_id = os.getenv("UPSTOX_CLIENT_ID", "")
    try:
        db_client_id = _db.get_setting("upstox_client_id", "")
    except Exception:
        db_client_id = ""
    valid_client_ids = {cid for cid in (conf_client_id, db_client_id) if cid and not cid.startswith("your_")}

    if req_client_id and valid_client_ids and req_client_id not in valid_client_ids:
        logger.warning("[Webhook Security] Rejected webhook with mismatched client_id: %s", req_client_id)
        raise HTTPException(status_code=403, detail="client_id does not match configured application")

    access_token = (payload.get("access_token") or "").strip()
    if not access_token:
        raise HTTPException(status_code=400, detail="Missing access_token in webhook payload")

    # Safe fingerprint calculation
    token_hash = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
    fingerprint = f"{token_hash[:6]}...{token_hash[-6:]}"
    logger.info("[Webhook Ingress] Received Upstox token notification: fingerprint=%s, length=%d", fingerprint, len(access_token))

    # Idempotency check: if already active and verified
    active_token = resolve_upstox_token()
    if active_token and hashlib.sha256(active_token.encode("utf-8")).hexdigest() == token_hash:
        logger.info("[Webhook Ingress] Token is already active and verified. Returning idempotent success.")
        return {
            "status": "success",
            "message": "Token already active and verified (idempotent)",
            "verified": True,
            "fingerprint": fingerprint,
        }

    # Verify token with Upstox API v2 user profile BEFORE persisting
    client = UpstoxClient(access_token=access_token)
    profile = client.get_profile()

    if profile and profile.get("status") == "success":
        user_data = profile.get("data", {})
        user_id = user_data.get("user_id", payload.get("user_id", ""))
        user_name = user_data.get("user_name", "")

        # Persist to SQLite
        try:
            _db.save_token(access_token)
            if user_id:
                _db.save_setting("upstox_user_id", user_id)
            if user_name:
                _db.save_setting("upstox_user_name", user_name)
        except Exception as e:
            logger.error("Failed to persist verified token to SQLite: %s", e)

        os.environ["UPSTOX_ACCESS_TOKEN"] = access_token
        _propagate_token_to_engine(access_token)
        _restart_websocket_client(access_token)

        logger.info("[Webhook Ingress] Successfully verified and persisted Upstox token for user: %s (%s)", user_name, user_id)
        return {
            "status": "success",
            "message": "Token verified, persisted, and runtime updated",
            "user_id": user_id,
            "user_name": user_name,
            "verified": True,
            "fingerprint": fingerprint,
        }
    else:
        logger.error("[Webhook Ingress] Verification failed against Upstox Profile API. Token NOT persisted.")
        raise HTTPException(
            status_code=401,
            detail="Token verification failed against Upstox /v2/user/profile. Token not persisted.",
        )

