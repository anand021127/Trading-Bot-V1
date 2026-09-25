"""Secret redaction guard for the Copilot.

Contract: no Copilot context, prompt, or answer may ever contain a
credential. Values that must NEVER leave the process toward an AI
provider:
  - UPSTOX_ACCESS_TOKEN (or any bearer/Upstox token)
  - OAuth client id / client secret
  - API keys
  - passwords, .env contents, Authorization headers

Redaction is value-based (exact known-secret values) AND key-based
(any dict key that looks like a credential), and recursively walks
arbitrary JSON-like structures. The same redaction is applied to the
final answer text so a misbehaving model cannot echo a secret back.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Set

# Keys whose values must never be emitted, by NAME alone (regardless of
# value). Kept explicit rather than clever — false negatives are far
# worse than false positives here.
SENSITIVE_KEY_PATTERNS = (
    "access_token", "refresh_token", "client_secret", "client_id",
    "api_key", "apikey", "authorization", "password", "secret",
    "credential", "bearer", "upstox_token", "private_key", "auth",
)

# Heuristic token-shape redaction for anything that slips in without a
# sensitive key name (e.g. a bearer token accidentally placed in a
# generic string field). Deliberately aggressive — "Bearer <anything>"
# is treated as a credential; over-redaction of prose is acceptable,
# under-redaction of a token is not.
_BEARER_RE = re.compile(r"(?i)bearer\s+\S+")
_LONG_HEX_RE = re.compile(r"\b[0-9a-f]{32,}\b", re.IGNORECASE)


def is_sensitive_key(key: str) -> bool:
    k = (key or "").lower()
    return any(p in k for p in SENSITIVE_KEY_PATTERNS)


def redact_text(text: str, secrets: Set[str]) -> str:
    """Replace every known secret VALUE with a fixed placeholder."""
    out = text
    for s in secrets:
        if s:
            out = out.replace(s, "[REDACTED]")
    out = _BEARER_RE.sub("Bearer [REDACTED]", out)
    out = _LONG_HEX_RE.sub("[REDACTED]", out)
    return out


def redact_value(value: Any, secrets: Set[str]) -> Any:
    """Recursively redact secrets from any JSON-like structure:
    dict keys that look like credentials are dropped, string values get
    known-secret values and token-shaped text replaced."""
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if is_sensitive_key(str(k)):
                continue
            out[k] = redact_value(v, secrets)
        return out
    if isinstance(value, list):
        return [redact_value(v, secrets) for v in value]
    if isinstance(value, tuple):
        return [redact_value(v, secrets) for v in value]
    if isinstance(value, str):
        return redact_text(value, secrets)
    return value


def collect_secret_values(secrets: List[str]) -> Set[str]:
    """Normalize the process's known secret VALUES into a lookup set.

    Called once per chat request from env + token store metadata — the
    values themselves never enter any context; only their redaction is
    used.
    """
    return {s for s in secrets if s and len(str(s)) >= 8}


def collect_process_secrets() -> List[str]:
    """The secret VALUES known to this process (env + token file), used
    ONLY for redaction. Never emitted anywhere."""
    vals = [
        os.getenv("UPSTOX_ACCESS_TOKEN", ""),
        os.getenv("UPSTOX_CLIENT_SECRET", ""),
        os.getenv("UPSTOX_API_KEY", ""),
        os.getenv("OPENAI_API_KEY", ""),
        os.getenv("COPILOT_AI_API_KEY", ""),
    ]
    token_file = os.path.join("data", "upstox_token.json")
    try:
        if os.path.exists(token_file):
            with open(token_file, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                vals.extend(str(v) for v in data.values() if isinstance(v, str))
    except Exception:
        pass
    return [v for v in vals if v]
