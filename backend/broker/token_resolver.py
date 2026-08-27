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
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


def persist_upstox_token(token: str, user_profile: Optional[Dict[str, Any]] = None) -> bool:
    """Canonical persistence function for Upstox access token.
    
    Atomically synchronizes the fresh token to:
    1. SQLite database (app_settings table)
    2. JSON token storage (/data/upstox_token.json, data/upstox_token.json)
    3. All accessible .env files (/home/ubuntu/Trading-Bot-V1/.env, <PROJECT_ROOT>/.env, cwd/.env)
    4. Runtime process memory (os.environ['UPSTOX_ACCESS_TOKEN'])
    """
    clean_token = (token or "").strip()
    if not clean_token:
        return False

    # 1. Update runtime memory immediately
    os.environ["UPSTOX_ACCESS_TOKEN"] = clean_token

    # 2. Persist to SQLite database
    try:
        from backend.database.db_manager import DatabaseManager
        db = DatabaseManager()
        db.save_token(clean_token)
        if user_profile:
            uname = user_profile.get("user_name") or user_profile.get("user_id") or ""
            uid = user_profile.get("user_id") or ""
            if uname:
                db.save_setting("upstox_user_name", uname)
            if uid:
                db.save_setting("upstox_user_id", uid)
    except Exception:
        pass

    # 3. Persist to JSON files
    payload = {
        "access_token": clean_token,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "source": "oauth_verified",
        "user_name": (user_profile or {}).get("user_name", ""),
        "user_id": (user_profile or {}).get("user_id", ""),
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


def resolve_upstox_token(
    explicit_token: Optional[str] = None,
    dotenv_path: Optional[str] = None,
) -> str:
    """Resolve Upstox access token with strict freshness awareness:
    1. Explicit runtime token (--token)
    2. Canonical SQLite DB / JSON persisted token (if active/fresh)
    3. os.environ['UPSTOX_ACCESS_TOKEN'] & repository .env file

    Automatically synchronizes valid tokens across storage targets.
    """
    # 1. Explicit runtime token
    if explicit_token and explicit_token.strip():
        tok = explicit_token.strip()
        os.environ["UPSTOX_ACCESS_TOKEN"] = tok
        return tok

    # 2. Check canonical SQLite database
    db_token = ""
    try:
        from backend.database.db_manager import DatabaseManager
        db = DatabaseManager()
        db_token = (db.load_token() or "").strip()
    except Exception:
        db_token = ""

    # 3. Check JSON storage
    json_token = ""
    for json_p in DEFAULT_TOKEN_JSON_PATHS:
        if os.path.isfile(json_p):
            try:
                with open(json_p, "r", encoding="utf-8") as f:
                    d = json.load(f)
                    jt = (d.get("access_token") or "").strip()
                    if jt:
                        json_token = jt
                        break
            except Exception:
                pass

    # 4. Check Environment & .env
    env_token = (os.getenv("UPSTOX_ACCESS_TOKEN") or "").strip()
    found_dotenv = find_repo_dotenv_path(dotenv_path)
    dot_token = ""
    if found_dotenv:
        env_vars = parse_dotenv_file(found_dotenv)
        dot_token = (env_vars.get("UPSTOX_ACCESS_TOKEN") or "").strip()

    # Determine candidate tokens
    candidates = [
        ("database", db_token),
        ("json", json_token),
        ("environment", env_token),
        ("dotenv", dot_token),
    ]

    # Filter non-empty candidates
    valid_candidates = [(src, tok) for src, tok in candidates if tok]

    if not valid_candidates:
        return ""

    # Evaluate freshness: if database or json has a newer / valid token, prefer it
    # If all are equal, return the first
    selected_token = valid_candidates[0][1]
    
    # Check if we have an unexpired JWT among candidates
    best_candidate = None
    for src, tok in valid_candidates:
        jwt_info = decode_jwt_safe(tok)
        if jwt_info.get("is_jwt"):
            exp = jwt_info.get("expires_at")
            now = time.time()
            if exp and exp > now:
                # Found active unexpired token
                best_candidate = (src, tok)
                break

    if best_candidate:
        selected_token = best_candidate[1]
    else:
        # If none unexpired, prioritize database / json over stale static env
        for src, tok in valid_candidates:
            if src in ("database", "json"):
                selected_token = tok
                break

    # Synchronize selected token to runtime memory
    if selected_token:
        os.environ["UPSTOX_ACCESS_TOKEN"] = selected_token

    return selected_token


def get_token_source(
    explicit_token: Optional[str] = None,
    dotenv_path: Optional[str] = None,
) -> str:
    """Determine the origin source of the active token."""
    if explicit_token and explicit_token.strip():
        return "runtime (--token)"

    try:
        from backend.database.db_manager import DatabaseManager
        db = DatabaseManager()
        db_token = (db.load_token() or "").strip()
        if db_token:
            return "database (SQLite)"
    except Exception:
        pass

    for json_p in DEFAULT_TOKEN_JSON_PATHS:
        if os.path.isfile(json_p):
            return f"json ({json_p})"

    env_token = os.getenv("UPSTOX_ACCESS_TOKEN", "")
    if env_token and env_token.strip():
        return "environment (os.environ)"

    found_dotenv = find_repo_dotenv_path(dotenv_path)
    if found_dotenv:
        env_vars = parse_dotenv_file(found_dotenv)
        dot_token = env_vars.get("UPSTOX_ACCESS_TOKEN", "")
        if dot_token and dot_token.strip():
            return f"dotenv ({found_dotenv})"

    return "none"


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
    """
    import urllib.request
    import urllib.error

    clean_token = (token or "").strip()
    if not clean_token:
        clean_token = resolve_upstox_token(dotenv_path=dotenv_path)

    if not clean_token:
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
        }

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
        if e.code == 401:
            result["error_code"] = "AUTH_INVALID_TOKEN"
            result["error_message"] = "Invalid or expired UPSTOX_ACCESS_TOKEN. Please generate a new active access token via OAuth."
            result["required_permission"] = "Valid, unexpired Upstox Access Token (refresh daily via OAuth login)"
        else:
            try:
                err_b = json.loads(e.read().decode("utf-8"))
                result["error_code"] = err_b.get("errors", [{}])[0].get("errorCode") or f"HTTP_{e.code}"
                result["error_message"] = err_b.get("errors", [{}])[0].get("message") or str(e)
            except Exception:
                result["error_code"] = f"HTTP_{e.code}"
                result["error_message"] = str(e)
    except Exception as e:
        result["error_code"] = "NETWORK_ERROR"
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
        if e.code == 403:
            result["expired_instruments_entitled"] = False
            result["plan_type"] = "Standard (Upstox Plus Plan Required for Expired Historical Derivatives)"
            if not result.get("error_code"):
                result["error_code"] = "PERMISSION_DENIED"
                result["error_message"] = "Access forbidden: Expired Instruments API requires active Upstox Plus Plan."
                result["required_permission"] = "Upstox Plus Plan subscription required for Expired Instruments historical derivatives API"
        elif e.code == 401:
            result["expired_instruments_entitled"] = False
            if not result.get("error_code"):
                result["error_code"] = "AUTH_INVALID_TOKEN"
                result["error_message"] = "Invalid or expired UPSTOX_ACCESS_TOKEN. Please generate a new active access token."
                result["required_permission"] = "Valid, unexpired Upstox Access Token (refresh daily via OAuth login)"
        else:
            result["expired_instruments_entitled"] = False
            if not result.get("error_code"):
                result["error_code"] = f"HTTP_{e.code}"
                result["error_message"] = f"Expired Instruments API returned HTTP {e.code}"
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
    result["message"] = result.get("error_message") or ("Upstox Plus Plan Active and verified." if is_accessible else "")

    if is_accessible:
        result["error_code"] = None
        result["error_message"] = None
        result["required_permission"] = None
    elif not result.get("error_code"):
        if not is_valid:
            result["error_code"] = "AUTH_INVALID_TOKEN"
            result["error_message"] = "Invalid or expired token."
            result["required_permission"] = "Valid, unexpired Upstox Access Token"
        elif not is_entitled:
            result["error_code"] = "PERMISSION_DENIED"
            result["error_message"] = "Upstox Plus Plan required for historical expired derivatives access."
            result["required_permission"] = "Upstox Plus Plan subscription"
        else:
            result["error_code"] = "ACCESS_CHECK_FAILED"
            result["error_message"] = "Upstox access verification failed."

    return result


