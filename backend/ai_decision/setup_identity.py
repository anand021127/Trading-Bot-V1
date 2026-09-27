"""Setup identity for AI decision deduplication (PHASE 5.2 §2/§3).

A "setup" is the continuing trading opportunity a V8-D signal represents.
It is NOT the same as the pipeline signal_id (which embeds the signal's
generation timestamp and therefore changes on every 2-second scan tick —
which used to cause repeated Ollama inference for the SAME setup).

The setup id is derived ONLY from verified, semantically meaningful inputs:

    strategy | underlying | direction | instrument_key | strike | expiry
    | last-candle timestamp (the market-state anchor) | identity version

Semantics:
  - Same setup rescanned while the same market bar is the newest  → SAME id
    (one AI inference, stored decision replayed on every scan).
  - A new bar forms / a different contract or direction is resolved → NEW id
    (a genuinely new opportunity gets a fresh AI evaluation).

Deliberately EXCLUDED: entry_price/LTP (ticks constantly — it would defeat
dedup), quantity/sizing (derived, not identity), risk state (changes are
covered by the input_snapshot_hash), signal generated_at (the noise this
module removes).
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

# Bump when the identity recipe itself changes; old stored decisions then
# never silently collide with new-evaluation results.
SETUP_IDENTITY_VERSION = "v1"


def _parse_ts(raw: Any) -> str:
    """Extract a stable string timestamp from a candle dict."""
    if isinstance(raw, dict):
        raw = raw.get("timestamp") or raw.get("time")
    if raw is None:
        return ""
    return str(raw)


def last_candle_timestamp(candles: List[Dict[str, Any]]) -> str:
    """Timestamp of the newest bar — the market-state anchor of the setup."""
    if not candles:
        return ""
    return _parse_ts(candles[-1])


def build_setup_id(
    *,
    signal: Any,
    contract: Dict[str, Any],
    expiry: str,
    candles: List[Dict[str, Any]],
) -> str:
    """Deterministic setup identity (sha256, 32 hex chars).

    Raises nothing; missing fields collapse to "" and produce a stable
    (if degenerate) id — callers gate on completeness before calling AI.
    """
    ind = getattr(signal, "indicators", None) or {}
    sel = ind.get("selected_contract") or {}
    direction = str(
        getattr(signal, "signal", "")
        or sel.get("option_type")
        or contract.get("option_type")
        or ""
    ).upper()
    raw = "|".join([
        SETUP_IDENTITY_VERSION,
        str(getattr(signal, "strategy_name", "") or ""),
        str(getattr(signal, "symbol", "") or ""),
        direction,
        str(contract.get("instrument_key") or sel.get("instrument_key") or ""),
        str(contract.get("strike") if contract.get("strike") is not None else sel.get("strike") or ""),
        str(expiry or sel.get("expiry") or ""),
        last_candle_timestamp(candles),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
