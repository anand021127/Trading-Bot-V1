"""Deterministic AI decision context + canonical input snapshot (PHASE 5.1 §3/§15).

The AI receives ONLY verified, system-computed values — never free-form
model-invented data, never secrets. The snapshot hash is computed over the
EXACT canonical JSON of what was sent to the model, so "why did the AI
approve this?" is reproducible from stored state alone.

Missing critical data is never fabricated: it is reported as present=false
and the decision engine's explicit policy turns critical gaps into WAIT/REJECT.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Indicator modules — the same production code the strategies use.
from backend.indicators.ema import calculate_ema
from backend.indicators.rsi import calculate_rsi
from backend.indicators.atr import calculate_atr
from backend.indicators.vwap import calculate_vwap

# V8-D pullback condition names (matched case-insensitively against the
# strategy's own conditions dict) so the AI's trend/pullback view comes
# from the strategy's verdict, not a re-implementation.
PULLBACK_COND_KEYS = ("pullback", "reversal", "confirmation")


def _f(value: Any) -> Optional[float]:
    """Strict positive float — 0/negative/NaN/inf mean 'not observed'."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= 0:
        return None
    return f


def _round(value: Optional[float], nd: int = 2) -> Optional[float]:
    return round(value, nd) if value is not None else None


# ── canonical snapshot ────────────────────────────────────────────────────

def canonicalize(value: Any) -> Any:
    """Canonicalize a JSON-able structure deterministically: dicts sorted by
    key, floats rounded to 6dp (stable across runs/platforms)."""
    if isinstance(value, dict):
        return {str(k): canonicalize(value[k]) for k in sorted(value.keys(), key=str)}
    if isinstance(value, (list, tuple)):
        return [canonicalize(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        f = float(value)
        if not math.isfinite(f):
            return None
        r = round(f, 6)
        return int(r) if r.is_integer() and abs(r) < 1e15 else r
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(canonicalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def snapshot_hash(snapshot: Dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON of exactly what the AI received."""
    return hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()


# ── secrets guard ─────────────────────────────────────────────────────────

_SECRET_KEY_MARKERS = (
    "token", "secret", "password", "api_key", "apikey", "authorization",
    "credential", "access_key", "client_id",
)


def assert_no_secrets(payload: Dict[str, Any]) -> None:
    """Fail loudly if a secret-looking key ever reaches the AI payload.

    Defense in depth: the builder below only copies whitelisted fields, but
    this guard makes a future accidental secret leak a hard error, and it is
    directly assertable in the regression suite (§22).
    """
    for key in payload:
        k = str(key).lower()
        if any(marker in k for marker in _SECRET_KEY_MARKERS):
            raise ValueError(f"SECRET_GUARD: key {key!r} must never be sent to the AI provider")
        if isinstance(payload[key], dict):
            assert_no_secrets(payload[key])


# ── risk/session context (built from the runtime's own state) ────────────

@dataclass
class RiskContext:
    equity: Optional[float] = None
    open_positions: int = 0
    trades_today: int = 0
    daily_realized_pnl: Optional[float] = None
    daily_loss_pct: Optional[float] = None
    kill_switch: bool = False
    kill_switch_level: str = "OFF"
    reconciliation_ok: bool = True
    max_positions: Optional[int] = None
    max_daily_trades: Optional[int] = None
    max_daily_loss_pct: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "equity": _round(self.equity),
            "open_positions": self.open_positions,
            "trades_today": self.trades_today,
            "daily_realized_pnl": _round(self.daily_realized_pnl),
            "daily_loss_pct": _round(self.daily_loss_pct, 4),
            "kill_switch_active": self.kill_switch,
            "kill_switch_level": self.kill_switch_level,
            "reconciliation_ok": self.reconciliation_ok,
            "max_positions": self.max_positions,
            "max_daily_trades": self.max_daily_trades,
            "max_daily_loss_pct": self.max_daily_loss_pct,
        }


@dataclass
class MarketSession:
    open: bool = False
    is_trading_day: bool = False
    label: str = "UNKNOWN"

    def to_dict(self) -> Dict[str, Any]:
        return {"market_open": self.open, "is_trading_day": self.is_trading_day, "label": self.label}


# ── the deterministic context builder ─────────────────────────────────────

def build_market_context(
    *,
    symbol: str,
    candles: List[Dict[str, Any]],
    signal: Any,
    contract: Dict[str, Any],
    expiry: str,
    risk: RiskContext,
    session: MarketSession,
    candles_fresh: bool,
    candle_age_seconds: Optional[float],
) -> Dict[str, Any]:
    """Compute the verified market/strategy/option context the AI will see.

    Every number here is derived from the SAME candles/chain the V8-D
    strategy just used — the AI cannot see anything the system cannot
    verify, and it cannot fetch anything itself.
    """
    ind = (getattr(signal, "indicators", None) or {})
    conditions = dict(getattr(signal, "conditions", None) or {})
    rejected = list(getattr(signal, "rejected_reasons", None) or [])

    closes = [_f(c.get("close")) for c in candles]
    closes = [c for c in closes if c is not None]
    highs = [_f(c.get("high")) for c in candles]
    lows = [_f(c.get("low")) for c in candles]
    volumes = [_f(c.get("volume")) for c in candles]

    def _series_ok(series: List[Optional[float]]) -> bool:
        return all(v is not None for v in series) and len(series) >= 2

    ema20_series = calculate_ema(closes, 20) if closes else []
    ema50_series = calculate_ema(closes, 50) if closes else []
    ema20 = _round(ema20_series[-1]) if ema20_series else None
    ema50 = _round(ema50_series[-1]) if ema50_series else None

    rsi_series = calculate_rsi(closes, 14) if closes else []
    rsi = _round(rsi_series[-1]) if rsi_series else None

    atr_series = calculate_atr(highs, lows, closes, 14) if (_series_ok(highs) and _series_ok(lows)) else []
    atr = _round(atr_series[-1]) if atr_series else None

    vwap_val = None
    if _series_ok(highs) and _series_ok(lows) and volumes and all(v is not None for v in volumes):
        vw = calculate_vwap(highs, lows, closes, volumes)
        vwap_val = _round(vw[-1]) if vw else None

    underlying_price = _f(ind.get("underlying_spot") or ind.get("spot_price"))
    last_close = closes[-1] if closes else None
    if underlying_price is None:
        underlying_price = last_close

    # Option-side values from the RESOLVED contract only.
    ltp = _f(contract.get("ltp") or contract.get("close_price") or getattr(signal, "entry_price", None))
    bid = _f(contract.get("bid_price") or contract.get("bid"))
    ask = _f(contract.get("ask_price") or contract.get("ask"))
    spread = None
    spread_pct = None
    if bid is not None and ask is not None and bid > 0 and ask >= bid:
        spread = round(ask - bid, 2)
        spread_pct = round((ask - bid) / bid * 100.0, 2)
    option_atr = _f(contract.get("option_atr") or contract.get("atr"))

    entry_price = _f(getattr(signal, "entry_price", None)) or ltp
    stop_loss = _f(getattr(signal, "stop_loss", None))
    target = _f(getattr(signal, "target", None))
    risk_reward = None
    if entry_price and stop_loss and target:
        risk_dist = entry_price - stop_loss
        reward_dist = target - entry_price
        if risk_dist > 0:
            risk_reward = round(reward_dist / risk_dist, 2)

    lot_size = None
    try:
        lot_size = int(contract.get("lot_size") or ind.get("lot_size") or 0) or None
    except (TypeError, ValueError):
        lot_size = None
    quantity = None
    sizing = ind.get("sizing") or {}
    try:
        quantity = int(sizing.get("quantity") or sizing.get("qty") or 0) or None
    except (TypeError, ValueError):
        quantity = None
    capital_used = None
    if entry_price and quantity:
        capital_used = round(entry_price * quantity, 2)
    risk_amount = None
    if entry_price and stop_loss and quantity and entry_price > stop_loss:
        risk_amount = round((entry_price - stop_loss) * quantity, 2)

    last_ts = None
    if candles:
        raw_ts = candles[-1].get("timestamp") or candles[-1].get("time")
        if isinstance(raw_ts, str) and raw_ts:
            last_ts = raw_ts

    # Pullback condition summary from the strategy's own conditions dict.
    pullback_conditions = {
        str(k): bool(v)
        for k, v in conditions.items()
        if any(pk in str(k).lower() for pk in PULLBACK_COND_KEYS)
    }

    return {
        "symbol": str(symbol),
        "underlying_price": _round(underlying_price),
        "candles_considered": len(candles),
        "last_close": _round(last_close),
        "ema20": ema20,
        "ema50": ema50,
        "rsi": rsi,
        "atr": atr,
        "vwap": vwap_val,
        "market_timestamp": last_ts or "",
        "data_freshness": {
            "candles_fresh": bool(candles_fresh),
            "candle_age_seconds": _round(candle_age_seconds, 1),
        },
        "strategy_conditions": conditions,
        "pullback_conditions": pullback_conditions,
        "rejected_reasons": rejected,
        "signal_direction": str(getattr(signal, "signal", "")),
        "option": {
            "option_type": str(contract.get("option_type") or ind.get("option_type") or ""),
            "strike": _f(contract.get("strike") or ind.get("atm_strike")),
            "expiry": str(expiry or contract.get("expiry") or ind.get("expiry_date") or ""),
            "instrument_key": str(contract.get("instrument_key") or ""),
            "ltp": _round(ltp),
            "bid": _round(bid),
            "ask": _round(ask),
            "spread": spread,
            "spread_pct": spread_pct,
            "option_atr": _round(option_atr),
            "lot_size": lot_size,
        },
        "proposed_trade": {
            "entry_price": _round(entry_price),
            "stop_loss": _round(stop_loss),
            "target": _round(target),
            "risk_reward": risk_reward,
            "quantity": quantity,
            "capital_used": capital_used,
            "risk_amount": risk_amount,
        },
        "risk": risk.to_dict(),
        "session": session.to_dict(),
    }


def build_ai_snapshot(context: Dict[str, Any], *, strategy: str) -> Dict[str, Any]:
    """The EXACT payload sent to the model — context + prompt template id.

    This dict (canonicalized) is what input_snapshot_hash covers. It must
    never contain secrets; assert_no_secrets runs before hashing.
    """
    snapshot = {
        "schema": "ai_trading_decision.v1",
        "strategy": str(strategy),
        "context": context,
    }
    assert_no_secrets(snapshot)
    return snapshot
