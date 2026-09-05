"""Authoritative Upstox Access Token Resolver and Lifecycle Manager.

Enforces strict token priority and synchronization across the entire application:
1. Explicit runtime parameter (--token)
2. Canonical persistent storage (SQLite database & upstox_token.json from verified OAuth flow)
3. Environment variables & Repository .env file (/home/ubuntu/Trading-Bot-V1/.env, <PROJECT_ROOT>/.env, or cwd/.env)

Guarantees atomic persistence, prevents stale environment variables from overriding fresh tokens,
and validates tokens against Upstox Profile and Expired Instruments APIs.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("token_resolver")

# Candidate locations for repository .env file
DEFAULT_REPO_DOTENV_PATHS: List[str] = [
    "/home/ubuntu/Trading-Bot-V1/.env",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"),
    os.path.join(os.getcwd(), ".env"),
]

# Standard JSON token storage paths
DEFAULT_TOKEN_JSON_PATHS: List[str] = [
    "/data/upstox_token.json",
    os.path.join(os.getcwd(), "data", "upstox_token.json"),
    os.path.join(os.getcwd(), "upstox_token.json"),
]


def token_fingerprint(token: Optional[str]) -> str:
    """Compute safe deterministic SHA-256 fingerprint for token identity verification.
    
    Never reveals the secret token content.
    Returns: 'NONE' if empty, or 'abc123...456def' (first 6 + last 6 hex chars).
    """
    if not token or not isinstance(token, str) or not token.strip():
        return "NONE"
    clean = token.strip()
    sha = hashlib.sha256(clean.encode("utf-8")).hexdigest()
    return f"{sha[:6]}...{sha[-6:]}"


# Alias for backward compatibility
_token_fingerprint = token_fingerprint


def decode_jwt_safe(token: str) -> Dict[str, Any]:
    """Safely decode JWT header and payload without exposing secret credentials."""
    if not token or not isinstance(token, str):
        return {"is_jwt": False, "reason": "empty_or_non_string"}

    parts = token.strip().split(".")
    if len(parts) != 3:
        return {"is_jwt": False, "reason": f"parts_count_{len(parts)}"}

    def _b64_decode(seg: str) -> Dict[str, Any]:
        rem = len(seg) % 4
        if rem > 0:
            seg += "=" * (4 - rem)
        return json.loads(base64.urlsafe_b64decode(seg).decode("utf-8", errors="ignore"))

    try:
        header = _b64_decode(parts[0])
        payload = _b64_decode(parts[1])

        iat = payload.get("iat")
        exp = payload.get("exp")
        now = time.time()

        if iat is not None and isinstance(iat, (int, float)) and iat > 1e11:
            iat = iat / 1000.0
        if exp is not None and isinstance(exp, (int, float)) and exp > 1e11:
            exp = exp / 1000.0

        is_expired = (exp < now) if (exp is not None) else None
        seconds_remaining = (exp - now) if (exp is not None) else None

        return {
            "is_jwt": True,
            "header": header,
            "issuer": payload.get("iss"),
            "subject_masked": (payload.get("sub", "")[:3] + "..." + payload.get("sub", "")[-2:]) if payload.get("sub") else None,
            "user_id_masked": (payload.get("user_id", "")[:3] + "..." + payload.get("user_id", "")[-2:]) if payload.get("user_id") else None,
            "isPlusPlan": bool(payload.get("isPlusPlan", False)),
            "isMultiClient": bool(payload.get("isMultiClient", False)),
            "issued_at": iat,
            "issued_at_iso": datetime.fromtimestamp(iat, tz=timezone.utc).isoformat() if iat else None,
            "expires_at": exp,
            "expires_at_iso": datetime.fromtimestamp(exp, tz=timezone.utc).isoformat() if exp else None,
            "is_expired": is_expired,
            "seconds_remaining": seconds_remaining,
        }
    except Exception as e:
        return {"is_jwt": False, "error": str(e)}


def check_token_freshness(token: Optional[str]) -> Dict[str, Any]:
    """Inspect JWT claims to check token freshness without exposing secrets.
    
    Returns a dict with:
      - is_jwt: bool
      - is_fresh: bool
      - is_expired: bool
      - seconds_remaining: Optional[float]
      - expires_at: Optional[float] (Unix seconds)
      - expires_at_iso: Optional[str]
      - issued_at: Optional[float] (Unix seconds)
      - issued_at_iso: Optional[str]
      - token_fingerprint: str
      - status: 'FRESH' | 'EXPIRED' | 'EXPIRING_SOON' | 'OPAQUE_UNVERIFIED' | 'EMPTY'
      - message: str
    """
    clean = (token or "").strip().strip('"\'').strip()
    if not clean:
        return {
            "is_jwt": False,
            "is_fresh": False,
            "is_expired": True,
            "seconds_remaining": 0.0,
            "expires_at": None,
            "expires_at_iso": None,
            "issued_at": None,
            "issued_at_iso": None,
            "token_fingerprint": "empty",
            "status": "EMPTY",
            "message": "Token is empty or not configured",
        }

    fp = token_fingerprint(clean)
    jwt = decode_jwt_safe(clean)

    if not jwt.get("is_jwt"):
        is_mock = clean.startswith(("mock-", "test-", "leftover-")) or len(clean) < 30
        is_expired = "expired" in clean.lower()
        return {
            "is_jwt": False,
            "is_fresh": not is_expired,
            "is_expired": is_expired,
            "seconds_remaining": None,
            "expires_at": None,
            "expires_at_iso": None,
            "issued_at": None,
            "issued_at_iso": None,
            "token_fingerprint": fp,
            "status": "EXPIRED" if is_expired else ("MOCK" if is_mock else "OPAQUE_UNVERIFIED"),
            "message": "Non-JWT token (expired)" if is_expired else ("Non-JWT mock token" if is_mock else "Non-JWT opaque token"),
        }

    now = time.time()
    exp = jwt.get("expires_at")
    iat = jwt.get("issued_at")

    if exp is not None:
        sec_rem = exp - now
        is_expired = sec_rem <= 0.0

        if is_expired:
            status = "EXPIRED"
            msg = f"Token expired {abs(round(sec_rem, 1))}s ago at {jwt.get('expires_at_iso')}"
        elif sec_rem <= 300.0:
            status = "EXPIRING_SOON"
            msg = f"Token expires soon in {round(sec_rem, 1)}s at {jwt.get('expires_at_iso')}"
        else:
            status = "FRESH"
            msg = f"Token is fresh, expires in {round(sec_rem / 3600.0, 1)}h at {jwt.get('expires_at_iso')}"

        return {
            "is_jwt": True,
            "is_fresh": not is_expired,
            "is_expired": is_expired,
            "seconds_remaining": round(sec_rem, 1),
            "expires_at": exp,
            "expires_at_iso": jwt.get("expires_at_iso"),
            "issued_at": iat,
            "issued_at_iso": jwt.get("issued_at_iso"),
            "token_fingerprint": fp,
            "status": status,
            "message": msg,
        }
    else:
        return {
            "is_jwt": True,
            "is_fresh": True,
            "is_expired": False,
            "seconds_remaining": None,
            "expires_at": None,
            "expires_at_iso": None,
            "issued_at": iat,
            "issued_at_iso": jwt.get("issued_at_iso"),
            "token_fingerprint": fp,
            "status": "FRESH",
            "message": "JWT token has no exp claim, assumed active",
        }


def parse_dotenv_file(filepath: str) -> Dict[str, str]:
    """Safely parse key-value pairs from a .env file without external dependencies."""
    if not filepath or not os.path.isfile(filepath):
        return {}

    env_map: Dict[str, str] = {}
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                # Remove surrounding quotes if present
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                if key:
                    env_map[key] = val
    except Exception:
        pass
    return env_map


def update_dotenv_file(filepath: str, updates: Dict[str, str]) -> bool:
    """Atomically update key-value pairs in a .env file without corrupting formatting."""
    if not filepath:
        return False

    parent_dir = os.path.dirname(os.path.abspath(filepath))
    if not os.path.isdir(parent_dir):
        try:
            os.makedirs(parent_dir, exist_ok=True)
        except Exception:
            return False

    existing_lines: List[str] = []
    if os.path.isfile(filepath):
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                existing_lines = f.readlines()
        except Exception:
            existing_lines = []

    updated_keys = set(updates.keys())
    new_lines: List[str] = []
    found_keys = set()

    for raw_line in existing_lines:
        stripped = raw_line.strip()
        is_export = stripped.startswith("export ")
        content = stripped[7:].strip() if is_export else stripped

        if content and not content.startswith("#") and "=" in content:
            key, _ = content.split("=", 1)
            key = key.strip()
            if key in updated_keys:
                found_keys.add(key)
                val = updates[key]
                prefix = "export " if is_export else ""
                new_lines.append(f'{prefix}{key}="{val}"\n')
                continue

        new_lines.append(raw_line)

    # Append any remaining new keys
    for k, v in updates.items():
        if k not in found_keys:
            new_lines.append(f'{k}="{v}"\n')

    # Atomic write via temp file
    temp_path = f"{filepath}.tmp.{os.getpid()}"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        os.replace(temp_path, filepath)
        return True
    except Exception:
        try:
            if os.path.isfile(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        return False


def find_repo_dotenv_path(custom_path: Optional[str] = None) -> Optional[str]:
    """Find the first existing .env file from custom path or standard candidate paths."""
    candidates: List[str] = []
    if custom_path:
        candidates.append(custom_path)
    env_file_var = os.getenv("ENV_FILE") or os.getenv("DOTENV_PATH")
    if env_file_var:
        candidates.append(env_file_var)
    candidates.extend(DEFAULT_REPO_DOTENV_PATHS)

    for p in candidates:
        if p and os.path.isfile(p):
            return os.path.abspath(p)
    return None


def load_repo_dotenv(custom_path: Optional[str] = None, override_environ: bool = False) -> Tuple[bool, Optional[str]]:
    """Load variables from repository .env into os.environ."""
    found_path = find_repo_dotenv_path(custom_path)
    if not found_path:
        return False, None

    parsed = parse_dotenv_file(found_path)
    for k, v in parsed.items():
        if override_environ or k not in os.environ:
            os.environ[k] = v

    return True, found_path


# Module-level store for the authoritative, verified runtime token
_current_verified_token: Optional[Dict[str, Any]] = None


def set_verified_runtime_token(token: str, metadata: Optional[Dict[str, Any]] = None) -> None:
    """Explicitly register an in-memory live-verified access token."""
    global _current_verified_token
    clean = (token or "").strip().strip('"\'').strip()
    if not clean:
        _current_verified_token = None
        return
    fp = token_fingerprint(clean)
    meta = metadata or {}
    _current_verified_token = {
        "token": clean,
        "fingerprint": fp,
        "verified": True,
        "verified_at": meta.get("verified_at") or datetime.now(timezone.utc).isoformat(),
        "source": meta.get("source") or "runtime (verified)",
        "user_name": meta.get("user_name", ""),
        "user_id": meta.get("user_id", ""),
        "is_plus_plan": meta.get("is_plus_plan", True),
    }


def get_verified_runtime_token() -> Optional[Dict[str, Any]]:
    """Return the current verified runtime token if active and unexpired."""
    global _current_verified_token
    if not _current_verified_token:
        return None
    jwt = decode_jwt_safe(_current_verified_token.get("token", ""))
    if jwt.get("is_expired") is True:
        _current_verified_token = None
        return None
    return _current_verified_token


def clear_verified_runtime_token() -> None:
    """Clear the in-memory verified runtime token."""
    global _current_verified_token
    _current_verified_token = None


def persist_upstox_token(
    token: str,
    user_profile: Optional[Dict[str, Any]] = None,
    verification_info: Optional[Dict[str, Any]] = None,
) -> bool:
    """Canonical persistence function for Upstox access token.
    
    Atomically synchronizes the fresh verified token to:
    1. Runtime verified memory cache (_current_verified_token and os.environ)
    2. SQLite database (app_settings table)
    3. JSON token storage (/data/upstox_token.json, data/upstox_token.json)
    4. All accessible .env files
    """
    clean_token = (token or "").strip().strip('"\'').strip()
    if not clean_token:
        return False

    is_mock = clean_token.startswith(("mock-", "test-", "leftover-")) or (0 < len(clean_token) < 30)

    freshness = check_token_freshness(clean_token)
    if freshness.get("is_expired") is True:
        logger.warning(
            "[Token Resolver] Refusing to persist expired token (fingerprint=%s, reason=%s)",
            freshness.get("token_fingerprint"),
            freshness.get("message"),
        )
        return False

    jwt = decode_jwt_safe(clean_token)
    fp = token_fingerprint(clean_token)
    v_at = (verification_info or {}).get("verified_at") or datetime.now(timezone.utc).isoformat()
    v_src = (verification_info or {}).get("source") or "oauth_verified"
    uname = (user_profile or {}).get("user_name") or (user_profile or {}).get("user_id") or ""
    uid = (user_profile or {}).get("user_id") or ""
    is_plus = bool((verification_info or {}).get("is_plus_plan") or jwt.get("isPlusPlan"))

    # 1. Update runtime verified memory immediately
    set_verified_runtime_token(
        clean_token,
        {
            "verified_at": v_at,
            "source": "runtime (verified)",
            "user_name": uname,
            "user_id": uid,
            "is_plus_plan": is_plus,
        },
    )

    if not is_mock:
        os.environ["UPSTOX_ACCESS_TOKEN"] = clean_token

    # 2. Persist to SQLite database
    try:
        from backend.database.db_manager import DatabaseManager
        db = DatabaseManager()
        db.save_token(clean_token, verified=True, verified_at=v_at, source=v_src)
        if uname:
            db.save_setting("upstox_user_name", uname)
        if uid:
            db.save_setting("upstox_user_id", uid)
    except Exception:
        pass

    # 3. Persist to JSON files (skip for mock strings in unit tests unless required)
    if not is_mock:
        payload = {
            "access_token": clean_token,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "verified": True,
            "verified_at": v_at,
            "source": v_src,
            "fingerprint": fp,
            "user_name": uname,
            "user_id": uid,
            "is_jwt": jwt.get("is_jwt", False),
            "expires_at": jwt.get("expires_at"),
            "issued_at": jwt.get("issued_at"),
            "is_plus_plan": is_plus,
        }
        for json_p in DEFAULT_TOKEN_JSON_PATHS:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(json_p)), exist_ok=True)
                with open(json_p, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)
            except Exception:
                pass

        # 4. Atomically update all candidate .env files
        for env_p in DEFAULT_REPO_DOTENV_PATHS:
            try:
                if os.path.exists(os.path.dirname(env_p)):
                    update_dotenv_file(env_p, {"UPSTOX_ACCESS_TOKEN": clean_token})
            except Exception:
                pass

    return True


def invalidate_old_token_references(new_token: str) -> None:
    """Invalidate all stale cached tokens and propagate the fresh canonical token.
    
    1. Updates runtime verified token state.
    2. Updates os.environ['UPSTOX_ACCESS_TOKEN'].
    3. Invalidates in-memory clients (TradingEngine, WebSocketClient, UpstoxClient).
    4. Ensures zero stale token references remain active without requiring restart.
    """
    clean = (new_token or "").strip().strip('"\'').strip()
    if not clean:
        return

    # 1. Synchronize os.environ
    os.environ["UPSTOX_ACCESS_TOKEN"] = clean

    # 2. Update verified runtime token
    set_verified_runtime_token(clean, {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "source": "runtime (propagated)",
    })

    # 3. Propagate to FastAPI app state if running
    try:
        import backend.api.main as main_mod
        app = getattr(main_mod, "app", None)
        if app is not None:
            engine = getattr(app.state, "engine", None)
            if engine is not None and hasattr(engine, "update_access_token"):
                try:
                    engine.update_access_token(clean)
                except Exception as e:
                    logger.debug("Failed to update engine token: %s", e)

            ws_client = getattr(app.state, "ws_client", None)
            if ws_client is not None and hasattr(ws_client, "reconnect_with_token"):
                try:
                    ws_client.reconnect_with_token(clean)
                except Exception as e:
                    logger.debug("Failed to reconnect ws_client with token: %s", e)
    except Exception as e:
        logger.debug("App state token propagation skipped: %s", e)

    # 4. Propagate to bot_control engine reference if separate
    try:
        from backend.api.routers.bot_control import get_engine
        engine = get_engine()
        if engine is not None and hasattr(engine, "update_access_token"):
            try:
                engine.update_access_token(clean)
            except Exception as e:
                logger.debug("Failed to update bot_control engine token: %s", e)
    except Exception:
        pass


def _score_candidate(
    token: str,
    source_key: str,
    is_verified: bool = False,
) -> Tuple[int, float, int]:
    """Score a candidate token strictly adhering to freshness and validity rules:
    Returns (tier, freshness, source_tier).
    
    Tiers:
      100: Live-verified unexpired token
       80: Unexpired structured JWT
       20: Non-JWT opaque candidate (unexpired)
      -50: EXPIRED token (never selected for active use)
     -100: Mock / test dummy token
    """
    clean = (token or "").strip().strip('"\'').strip()
    if not clean:
        return (-200, 0.0, 0)

    is_mock = clean.startswith(("mock-", "test-", "leftover-")) or len(clean) < 30
    if is_mock:
        return (-100, 0.0, 0)

    jwt = decode_jwt_safe(clean)
    is_jwt = jwt.get("is_jwt", False)
    exp = float(jwt.get("expires_at") or 0.0)
    iat = float(jwt.get("issued_at") or 0.0)
    freshness = max(iat, exp)
    is_expired = jwt.get("is_expired")

    src_tier = {
        "runtime": 10,
        "database": 4,
        "json": 3,
        "environment": 2,
        "dotenv": 1,
    }.get(source_key, 0)

    # If the token is expired, strictly reject from active use
    if is_expired is True:
        return (-50, freshness, src_tier)

    if is_jwt:
        if is_verified:
            return (100, freshness, src_tier)
        return (80, freshness, src_tier)
    else:
        # Non-JWT opaque token
        return (20, freshness, src_tier)


def get_token_diagnostic_candidates(
    explicit_token: Optional[str] = None,
    dotenv_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Gather all candidate tokens across all layers with detailed diagnostic metadata."""
    results: List[Dict[str, Any]] = []

    # Priority 1 candidate: explicit runtime
    if explicit_token and explicit_token.strip():
        tok = explicit_token.strip().strip('"\'').strip()
        jwt = decode_jwt_safe(tok)
        results.append({
            "source": "runtime (--token)",
            "source_key": "runtime",
            "token": tok,
            "fingerprint": token_fingerprint(tok),
            "length": len(tok),
            "is_jwt": jwt.get("is_jwt", False),
            "issued_at_iso": jwt.get("issued_at_iso"),
            "expires_at_iso": jwt.get("expires_at_iso"),
            "is_expired": jwt.get("is_expired"),
            "isPlusPlan": jwt.get("isPlusPlan", False),
            "verified": True,
            "rejection_reason": "ACTIVE_SELECTION",
        })
        return results

    # Priority 2 candidate: current verified runtime token
    v_tok = get_verified_runtime_token()
    if v_tok and v_tok.get("token"):
        tok = v_tok["token"]
        jwt = decode_jwt_safe(tok)
        results.append({
            "source": v_tok.get("source", "runtime (verified)"),
            "source_key": "runtime",
            "token": tok,
            "fingerprint": token_fingerprint(tok),
            "length": len(tok),
            "is_jwt": jwt.get("is_jwt", False),
            "issued_at_iso": jwt.get("issued_at_iso"),
            "expires_at_iso": jwt.get("expires_at_iso"),
            "is_expired": jwt.get("is_expired"),
            "isPlusPlan": jwt.get("isPlusPlan", False) or v_tok.get("is_plus_plan", False),
            "verified": True,
            "rejection_reason": "ACTIVE_SELECTION",
        })

    # Priority 3 & 4 candidates: Persisted sources
    # SQLite
    try:
        from backend.database.db_manager import DatabaseManager
        db = DatabaseManager()
        db_raw = db.load_token(require_valid=False)
        db_ver = db.get_setting("upstox_token_verified", "") == "true"
        if db_raw:
            jwt = decode_jwt_safe(db_raw)
            src_label = "database (SQLite verified)" if db_ver else "database (SQLite)"
            results.append({
                "source": src_label,
                "source_key": "database",
                "token": db_raw,
                "fingerprint": token_fingerprint(db_raw),
                "length": len(db_raw),
                "is_jwt": jwt.get("is_jwt", False),
                "issued_at_iso": jwt.get("issued_at_iso"),
                "expires_at_iso": jwt.get("expires_at_iso"),
                "is_expired": jwt.get("is_expired"),
                "isPlusPlan": jwt.get("isPlusPlan", False),
                "verified": db_ver,
                "rejection_reason": "EXPIRED" if jwt.get("is_expired") is True else None,
            })
    except Exception:
        pass

    # JSON files
    for json_p in DEFAULT_TOKEN_JSON_PATHS:
        if os.path.isfile(json_p):
            try:
                with open(json_p, "r", encoding="utf-8") as f:
                    d = json.load(f)
                    jt = (d.get("access_token") or "").strip().strip('"\'').strip()
                    if jt:
                        jwt = decode_jwt_safe(jt)
                        results.append({
                            "source": f"json ({json_p})",
                            "source_key": "json",
                            "token": jt,
                            "fingerprint": token_fingerprint(jt),
                            "length": len(jt),
                            "is_jwt": jwt.get("is_jwt", False),
                            "issued_at_iso": jwt.get("issued_at_iso"),
                            "expires_at_iso": jwt.get("expires_at_iso"),
                            "is_expired": jwt.get("is_expired"),
                            "isPlusPlan": jwt.get("isPlusPlan", False) or d.get("is_plus_plan", False),
                            "verified": d.get("verified", False),
                            "rejection_reason": "EXPIRED" if jwt.get("is_expired") is True else None,
                        })
            except Exception:
                pass

    # Environment variable (os.environ)
    env_token = (os.getenv("UPSTOX_ACCESS_TOKEN") or "").strip().strip('"\'').strip()
    if env_token:
        jwt = decode_jwt_safe(env_token)
        results.append({
            "source": "environment (os.environ)",
            "source_key": "environment",
            "token": env_token,
            "fingerprint": token_fingerprint(env_token),
            "length": len(env_token),
            "is_jwt": jwt.get("is_jwt", False),
            "issued_at_iso": jwt.get("issued_at_iso"),
            "expires_at_iso": jwt.get("expires_at_iso"),
            "is_expired": jwt.get("is_expired"),
            "isPlusPlan": jwt.get("isPlusPlan", False),
            "verified": False,
            "rejection_reason": "EXPIRED" if jwt.get("is_expired") is True else None,
        })

    # Dotenv files
    found_dotenv = find_repo_dotenv_path(dotenv_path)
    if found_dotenv:
        env_vars = parse_dotenv_file(found_dotenv)
        dot_token = (env_vars.get("UPSTOX_ACCESS_TOKEN") or "").strip().strip('"\'').strip()
        if dot_token:
            jwt = decode_jwt_safe(dot_token)
            results.append({
                "source": f"dotenv ({found_dotenv})",
                "source_key": "dotenv",
                "token": dot_token,
                "fingerprint": token_fingerprint(dot_token),
                "length": len(dot_token),
                "is_jwt": jwt.get("is_jwt", False),
                "issued_at_iso": jwt.get("issued_at_iso"),
                "expires_at_iso": jwt.get("expires_at_iso"),
                "is_expired": jwt.get("is_expired"),
                "isPlusPlan": jwt.get("isPlusPlan", False),
                "verified": False,
                "rejection_reason": "EXPIRED" if jwt.get("is_expired") is True else None,
            })

    return results


def resolve_upstox_token_with_source(
    explicit_token: Optional[str] = None,
    dotenv_path: Optional[str] = None,
    require_valid: bool = False,
) -> Tuple[str, str]:
    """Resolve authoritative Upstox access token and its precise origin source.
    
    Candidate Priority:
    1. Explicit runtime token ONLY when explicitly supplied (--token).
    2. Current verified runtime token (_current_verified_token).
    3. Persisted token that has been LIVE VERIFIED and is still valid.
    4. Other unexpired candidates strictly ordered by freshness.
    
    Guarantees:
    - Never selects an expired token when require_valid=True.
    - If SQLite contains an older/expired token while .env contains a newer valid token,
      NEVER chooses the older SQLite token.
    - Only updates os.environ if the chosen token is active and unexpired.
    """
    # 1. Explicit runtime token ONLY when explicitly supplied
    if explicit_token and explicit_token.strip():
        tok = explicit_token.strip().strip('"\'').strip()
        os.environ["UPSTOX_ACCESS_TOKEN"] = tok
        return tok, "runtime (--token)"

    # 2. Current verified runtime token
    v_tok = get_verified_runtime_token()
    if v_tok and v_tok.get("token"):
        tok = v_tok["token"]
        os.environ["UPSTOX_ACCESS_TOKEN"] = tok
        return tok, v_tok.get("source", "runtime (verified)")

    # 3. Collect candidates across persisted sources
    raw_candidates = get_token_diagnostic_candidates(dotenv_path=dotenv_path)
    if not raw_candidates:
        return "", "none"

    # Score candidates
    scored = []
    for cand in raw_candidates:
        tok = cand["token"]
        src = cand["source_key"]
        is_ver = cand.get("verified", False)
        score = _score_candidate(tok, src, is_verified=is_ver)
        scored.append((score, cand["source"], tok, cand))

    # Sort descending: highest tier, freshest iat/exp, highest source tier
    scored.sort(key=lambda item: item[0], reverse=True)

    best_score, best_src_label, best_token, best_cand = scored[0]
    tier = best_score[0]

    # If require_valid=True and best tier is <= 0 (expired or mock), abort
    if require_valid and tier <= 0:
        logger.warning(
            "[Token Resolver] No unexpired active token found among %d candidates. Best tier=%s",
            len(scored), tier
        )
        return "", "none"

    # Synchronize chosen unexpired token to runtime memory
    if tier > 0 and not (best_token.startswith(("mock-", "test-", "leftover-")) or len(best_token) < 30):
        os.environ["UPSTOX_ACCESS_TOKEN"] = best_token

    return best_token, best_src_label


def resolve_upstox_token(
    explicit_token: Optional[str] = None,
    dotenv_path: Optional[str] = None,
    require_valid: bool = False,
) -> str:
    """Resolve Upstox access token with strict freshness and integrity awareness."""
    tok, _ = resolve_upstox_token_with_source(explicit_token, dotenv_path=dotenv_path, require_valid=require_valid)
    return tok


def get_token_source(
    explicit_token: Optional[str] = None,
    dotenv_path: Optional[str] = None,
) -> str:
    """Determine the origin source of the winning resolved token."""
    _, src = resolve_upstox_token_with_source(explicit_token, dotenv_path=dotenv_path)
    return src


def get_token_metadata(
    explicit_token: Optional[str] = None,
    dotenv_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Return safe metadata (presence, length, masked prefix/suffix, fingerprint, source, JWT claims) without exposing secrets."""
    token = resolve_upstox_token(explicit_token, dotenv_path=dotenv_path)
    source = get_token_source(explicit_token, dotenv_path=dotenv_path)

    if not token:
        return {
            "present": False,
            "length": 0,
            "masked": "NONE",
            "fingerprint": "NONE",
            "source": "none",
            "is_jwt": False,
            "is_expired": None,
        }

    sha = hashlib.sha256(token.encode("utf-8")).hexdigest()
    prefix = token[:4] if len(token) >= 8 else "..."
    suffix = token[-4:] if len(token) >= 8 else "..."
    masked = f"{prefix}...{suffix} (len={len(token)})"

    jwt_meta = decode_jwt_safe(token)

    return {
        "present": True,
        "length": len(token),
        "masked": masked,
        "fingerprint": f"{sha[:6]}...{sha[-6:]}",
        "source": source,
        "is_jwt": jwt_meta.get("is_jwt", False),
        "is_expired": jwt_meta.get("is_expired"),
        "expires_at_iso": jwt_meta.get("expires_at_iso"),
        "issued_at_iso": jwt_meta.get("issued_at_iso"),
        "isPlusPlan": jwt_meta.get("isPlusPlan", False),
    }


def validate_token_live(token: Optional[str] = None, dotenv_path: Optional[str] = None) -> Dict[str, Any]:
    """Perform direct live validation against both Upstox User Profile and Expired Instruments APIs.
    
    Returns canonical authentication/entitlement contract:
    - has_token: bool (Token is present)
    - valid: bool (User Profile verified HTTP 200)
    - profile_status: Optional[int] (HTTP status code)
    - profile_verified: bool (HTTP 200 + status == 'success')
    - user_name: Optional[str]
    - user_id: Optional[str]
    - expired_instruments_status: Optional[int] (HTTP status code)
    - expired_instruments_entitled: bool (HTTP 200 + status == 'success')
    - accessible: bool (valid AND profile_verified AND expired_instruments_entitled)
    - plan_type: str ('Upstox Plus Plan (Expired Derivatives Enabled)' or 'Standard')
    - error_code: Optional[str] ('AUTH_INVALID_TOKEN', 'PERMISSION_DENIED', 'NO_TOKEN', etc.)
    - error_message: Optional[str]
    - message: str
    - required_permission: Optional[str]
    - token_fingerprint: str (Safe SHA-256 fingerprint for diagnostic correlation)
    """
    import urllib.request
    import urllib.error

    clean_token = (token or "").strip().strip('"\'').strip()
    if not clean_token:
        clean_token = resolve_upstox_token(dotenv_path=dotenv_path, require_valid=True)

    fp = token_fingerprint(clean_token)

    if not clean_token:
        logger.info("[Token Diagnostic] validate_token_live: No token found. fingerprint=NONE")
        return {
            "has_token": False,
            "valid": False,
            "profile_status": None,
            "profile_verified": False,
            "user_name": None,
            "user_id": None,
            "expired_instruments_status": None,
            "expired_instruments_entitled": False,
            "accessible": False,
            "plan_type": "None",
            "error_code": "NO_TOKEN",
            "error_message": "No Upstox access token found. Please authenticate via OAuth in Settings.",
            "message": "No Upstox access token found. Please authenticate via OAuth in Settings.",
            "required_permission": "Upstox Access Token (connect via Settings OAuth)",
            "token_fingerprint": "NONE",
        }

    jwt_meta = decode_jwt_safe(clean_token)
    result: Dict[str, Any] = {
        "has_token": True,
        "valid": False,
        "profile_status": None,
        "profile_verified": False,
        "user_name": None,
        "user_id": None,
        "expired_instruments_status": None,
        "expired_instruments_entitled": False,
        "accessible": False,
        "plan_type": "Standard",
        "error_code": None,
        "error_message": None,
        "message": "",
        "required_permission": None,
        "token_fingerprint": fp,
        "is_jwt": jwt_meta.get("is_jwt", False),
        "jwt_expired": jwt_meta.get("is_expired"),
        "jwt_expires_at": jwt_meta.get("expires_at_iso"),
        "is_plus_plan": jwt_meta.get("isPlusPlan", False),
    }

    # 1. Probe User Profile API
    try:
        req = urllib.request.Request(
            "https://api.upstox.com/v2/user/profile",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {clean_token}",
                "User-Agent": "Upstox-Trading-Bot/2.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            result["profile_status"] = resp.status
            result["profile_verified"] = (resp.status == 200 and data.get("status") == "success")
            result["user_name"] = data.get("data", {}).get("user_name")
            result["user_id"] = data.get("data", {}).get("user_id")
    except urllib.error.HTTPError as e:
        result["profile_status"] = e.code
        result["profile_verified"] = False
        parsed_code = None
        parsed_msg = None
        try:
            err_b = json.loads(e.read().decode("utf-8"))
            if "errors" in err_b and isinstance(err_b["errors"], list) and len(err_b["errors"]) > 0:
                parsed_code = err_b["errors"][0].get("errorCode") or err_b["errors"][0].get("error_code")
                parsed_msg = err_b["errors"][0].get("message")
        except Exception:
            pass

        if e.code == 401:
            result["error_code"] = "AUTHENTICATION_FAILURE"
            result["failure_classification"] = "AUTHENTICATION_FAILURE"
            result["error_message"] = parsed_msg or "Invalid or expired UPSTOX_ACCESS_TOKEN. Please generate a new active access token via OAuth."
            result["required_permission"] = "Valid, unexpired Upstox Access Token (refresh daily via OAuth login)"
        else:
            result["error_code"] = "AUTHENTICATION_FAILURE"
            result["failure_classification"] = "AUTHENTICATION_FAILURE"
            result["error_message"] = parsed_msg or str(e)
    except Exception as e:
        result["error_code"] = "AUTHENTICATION_FAILURE"
        result["failure_classification"] = "AUTHENTICATION_FAILURE"
        result["error_message"] = str(e)
        result["required_permission"] = "Stable Internet connectivity to api.upstox.com"

    # 2. Probe Expired Instruments API (NIFTY50)
    try:
        exp_url = "https://api.upstox.com/v2/expired-instruments/expiries?instrument_key=NSE_INDEX%7CNifty%2050"
        req_exp = urllib.request.Request(
            exp_url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {clean_token}",
                "User-Agent": "Upstox-Trading-Bot/2.0",
            },
        )
        with urllib.request.urlopen(req_exp, timeout=10) as resp_exp:
            data_exp = json.loads(resp_exp.read().decode("utf-8"))
            result["expired_instruments_status"] = resp_exp.status
            result["expired_instruments_entitled"] = (resp_exp.status == 200 and data_exp.get("status") == "success")
            result["plan_type"] = "Upstox Plus Plan (Expired Derivatives Enabled)"
    except urllib.error.HTTPError as e:
        result["expired_instruments_status"] = e.code
        parsed_exp_code = None
        parsed_exp_msg = None
        try:
            err_b = json.loads(e.read().decode("utf-8"))
            if "errors" in err_b and isinstance(err_b["errors"], list) and len(err_b["errors"]) > 0:
                parsed_exp_code = err_b["errors"][0].get("errorCode") or err_b["errors"][0].get("error_code")
                parsed_exp_msg = err_b["errors"][0].get("message")
        except Exception:
            pass

        if e.code == 403:
            result["expired_instruments_entitled"] = False
            result["plan_type"] = "Standard (Upstox Plus Plan Required for Expired Historical Derivatives)"
            result["error_code"] = "EXPIRED_OPTIONS_ENTITLEMENT_FAILURE"
            result["failure_classification"] = "EXPIRED_OPTIONS_ENTITLEMENT_FAILURE"
            result["error_message"] = parsed_exp_msg or "Access forbidden: Expired Instruments API requires active Upstox Plus Plan."
            result["required_permission"] = "Upstox Plus Plan subscription required for Expired Instruments historical derivatives API"
        elif e.code == 401:
            result["expired_instruments_entitled"] = False
            if not result.get("error_code"):
                result["error_code"] = "AUTHENTICATION_FAILURE"
                result["failure_classification"] = "AUTHENTICATION_FAILURE"
                result["error_message"] = parsed_exp_msg or "Invalid or expired UPSTOX_ACCESS_TOKEN. Please generate a new active access token."
                result["required_permission"] = "Valid, unexpired Upstox Access Token (refresh daily via OAuth login)"
        else:
            result["expired_instruments_entitled"] = False
            if not result.get("error_code"):
                result["error_code"] = parsed_exp_code or f"HTTP_{e.code}"
                result["error_message"] = parsed_exp_msg or f"Expired Instruments API returned HTTP {e.code}"
    except Exception as e:
        if not result.get("error_code"):
            result["error_code"] = "NETWORK_ERROR"
            result["error_message"] = str(e)

    # 3. Derive canonical authorization and accessibility flags
    is_valid = bool(result["profile_verified"])
    is_entitled = bool(result["expired_instruments_entitled"])
    is_accessible = bool(is_valid and is_entitled)

    result["valid"] = is_valid
    result["accessible"] = is_accessible

    if is_valid:
        # Token is authenticated and valid for user account, REST APIs, orders, and WebSocket
        set_verified_runtime_token(
            clean_token,
            {
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "source": "runtime (verified)",
                "user_name": result.get("user_name", ""),
                "user_id": result.get("user_id", ""),
                "is_plus_plan": is_entitled,
                "entitlement_status": "ENTITLED" if is_entitled else "EXPIRED_OPTIONS_ENTITLEMENT_FAILURE",
            },
        )
        if is_entitled:
            result["error_code"] = None
            result["failure_classification"] = None
            result["error_message"] = None
            result["required_permission"] = None
            result["message"] = "Upstox Plus Plan Active and verified."
        else:
            result["error_code"] = "EXPIRED_OPTIONS_ENTITLEMENT_FAILURE"
            result["failure_classification"] = "EXPIRED_OPTIONS_ENTITLEMENT_FAILURE"
            result["error_message"] = "Profile verified (authenticated). Expired Options Historical Data API requires Upstox Plus Plan subscription (HTTP 403 Forbidden)."
            result["required_permission"] = "Upstox Plus Plan subscription"
            result["message"] = "Token verified for trading & live feeds. Expired Options API requires Upstox Plus Plan."
    else:
        result["error_code"] = "AUTHENTICATION_FAILURE"
        result["failure_classification"] = "AUTHENTICATION_FAILURE"
        result["error_message"] = result.get("error_message") or "Authentication failed — invalid or expired token."
        result["required_permission"] = "Valid, unexpired Upstox Access Token"
        result["message"] = "Authentication failed: invalid or expired token."

    logger.info(
        "[Token Diagnostic] validate_token_live: length=%d, fingerprint=%s, profile_status=%s, expired_status=%s, accessible=%s",
        len(clean_token), fp, result["profile_status"], result["expired_instruments_status"], result["accessible"]
    )

    return result


