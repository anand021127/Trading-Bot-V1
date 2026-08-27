"""Authoritative Upstox Access Token Resolver.

Enforces strict token priority across the entire application:
1. Explicit runtime parameter (--token)
2. Environment variable (os.environ['UPSTOX_ACCESS_TOKEN'])
3. Repository .env file (/home/ubuntu/Trading-Bot-V1/.env, <PROJECT_ROOT>/.env, or cwd/.env)
4. SQLite Database token (persisted via OAuth callback)
"""
from __future__ import annotations

import os
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Candidate locations for repository .env file
DEFAULT_REPO_DOTENV_PATHS: List[str] = [
    "/home/ubuntu/Trading-Bot-V1/.env",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"),
    os.path.join(os.getcwd(), ".env"),
]


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
    """Load variables from repository .env into os.environ (if not already set)."""
    found_path = find_repo_dotenv_path(custom_path)
    if not found_path:
        return False, None

    parsed = parse_dotenv_file(found_path)
    for k, v in parsed.items():
        if override_environ or k not in os.environ:
            os.environ[k] = v

    return True, found_path


def resolve_upstox_token(
    explicit_token: Optional[str] = None,
    dotenv_path: Optional[str] = None,
) -> str:
    """Resolve Upstox access token with strict priority:
    1. Explicit runtime token (--token)
    2. Environment variable (UPSTOX_ACCESS_TOKEN in os.environ)
    3. Repository .env file (/home/ubuntu/Trading-Bot-V1/.env, <PROJECT_ROOT>/.env, etc.)
    4. SQLite database token (persisted via OAuth callback)
    """
    # 1. Explicit runtime token
    if explicit_token and explicit_token.strip():
        return explicit_token.strip()

    # 2. Environment variable
    env_token = os.getenv("UPSTOX_ACCESS_TOKEN", "")
    if env_token and env_token.strip():
        return env_token.strip()

    # 3. Repository .env file
    found_dotenv = find_repo_dotenv_path(dotenv_path)
    if found_dotenv:
        env_vars = parse_dotenv_file(found_dotenv)
        dot_token = env_vars.get("UPSTOX_ACCESS_TOKEN", "")
        if dot_token and dot_token.strip():
            # Populate into os.environ for other components
            os.environ["UPSTOX_ACCESS_TOKEN"] = dot_token.strip()
            return dot_token.strip()

    # 4. SQLite database fallback
    try:
        from backend.database.db_manager import DatabaseManager
        db = DatabaseManager()
        db_token = db.load_token()
        if db_token and db_token.strip():
            return db_token.strip()
    except Exception:
        pass

    return ""


def get_token_source(
    explicit_token: Optional[str] = None,
    dotenv_path: Optional[str] = None,
) -> str:
    """Determine the origin source of the active token."""
    if explicit_token and explicit_token.strip():
        return "runtime (--token)"

    env_token = os.getenv("UPSTOX_ACCESS_TOKEN", "")
    if env_token and env_token.strip():
        return "environment (os.environ)"

    found_dotenv = find_repo_dotenv_path(dotenv_path)
    if found_dotenv:
        env_vars = parse_dotenv_file(found_dotenv)
        dot_token = env_vars.get("UPSTOX_ACCESS_TOKEN", "")
        if dot_token and dot_token.strip():
            return f"dotenv ({found_dotenv})"

    try:
        from backend.database.db_manager import DatabaseManager
        db = DatabaseManager()
        db_token = db.load_token()
        if db_token and db_token.strip():
            return "database (SQLite)"
    except Exception:
        pass

    return "none"


def get_token_metadata(
    explicit_token: Optional[str] = None,
    dotenv_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Return safe metadata (presence, length, masked prefix/suffix, fingerprint, source) without exposing secrets."""
    token = resolve_upstox_token(explicit_token, dotenv_path=dotenv_path)
    source = get_token_source(explicit_token, dotenv_path=dotenv_path)

    if not token:
        return {
            "present": False,
            "length": 0,
            "masked": "NONE",
            "fingerprint": "NONE",
            "source": "none",
        }

    sha = hashlib.sha256(token.encode("utf-8")).hexdigest()
    prefix = token[:4] if len(token) >= 8 else "..."
    suffix = token[-4:] if len(token) >= 8 else "..."
    masked = f"{prefix}...{suffix} (len={len(token)})"

    return {
        "present": True,
        "length": len(token),
        "masked": masked,
        "fingerprint": f"{sha[:6]}...{sha[-6:]}",
        "source": source,
    }

