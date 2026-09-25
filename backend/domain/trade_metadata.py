"""The ONE common trade metadata contract.

Every execution mode — PAPER, LIVE, BACKTEST — records trades through this
single model so Trade History always knows the same things:

    WHAT was traded, WHICH STRIKE, WHICH OPTION (CE/PE), WHICH EXPIRY,
    HOW MANY QUANTITY, WHAT LOT SIZE, HOW MUCH CAPITAL WAS USED,
    WHAT PRICE, WHAT P&L.

Design rules (production-hard, enforced by ``normalize_trade_metadata``):

1. Capital used is ALWAYS ``entry_price × executed_quantity`` — never
   allocation, never max theoretical risk, never account capital.
2. Nothing is ever invented. Metadata that genuinely does not exist (e.g.
   historical rows written before this contract) stays None and the UI
   renders ``N/A / Historical metadata unavailable``.
3. There is exactly one schema (the ``trades`` table columns). Paper, Live
   and Backtest all write/read the SAME columns through the same helpers —
   no mode-specific schema exists anywhere.
4. Authoritative contract metadata only: strike/option_type/expiry/lot_size
   must come from the resolved broker/instrument contract (or the backtest's
   actual historical contract), never be guessed from the symbol name.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# The single canonical field list. Column names in the trades table match
# these keys exactly (stored snake_case, e.g. strike_price).
TRADE_METADATA_FIELDS = (
    "underlying_symbol",
    "option_type",
    "strike_price",
    "expiry",
    "instrument_key",
    "entry_price",
    "exit_price",
    "quantity",
    "lot_size",
    "capital_used",
    "entry_timestamp",
    "exit_timestamp",
    "strategy",
    "status",
)

# Internal bookkeeping fields carried alongside the contract metadata.
_ID_FIELDS = ("trade_id", "order_id", "signal_id")

# Signal/contract payload keys accepted as aliases for the canonical fields.
# This lets paper signals, live contracts and backtest trade dicts feed the
# same normalizer without each mode pre-mapping its own vocabulary.
_ALIASES = {
    "underlying_symbol": ("underlying_symbol", "underlying", "symbol"),
    "option_type": ("option_type",),
    "strike_price": ("strike_price", "strike"),
    "expiry": ("expiry", "expiry_date"),
    "instrument_key": ("instrument_key", "contract_instrument_key", "contract_key"),
    "lot_size": ("lot_size",),
}

_HISTORICAL_NOTE = "N/A / Historical metadata unavailable"


def _first(source: Dict[str, Any], *keys: str) -> Any:
    """First non-None, non-empty-string value among the alias keys."""
    for k in keys:
        v = source.get(k)
        if v is not None and not (isinstance(v, str) and not v.strip()):
            return v
    return None


def _as_float(v: Any) -> Optional[float]:
    """Strict float coercion. 0 / negative / non-numeric → None (no invention).

    An option premium is strictly positive; 0 is treated as "no valid price
    observed" rather than a real price, consistent with the paper exit path.
    """
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")) or f <= 0.0:
        return None
    return f


def _as_int(v: Any) -> Optional[int]:
    """Strict non-negative int coercion (quantity / lot size)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")) or f < 0.0 or f != int(f):
        return None
    return int(f)


def _as_text(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def compute_capital_used(entry_price: Any, executed_quantity: Any) -> Optional[float]:
    """capital_used = entry_price × executed_quantity — the ONLY definition.

    Returns None when either input is not a valid positive price / non-negative
    executed quantity (fail-safe: callers then persist NULL rather than a
    fabricated capital figure).
    """
    price = _as_float(entry_price)
    qty = _as_int(executed_quantity)
    if price is None or qty is None or qty == 0:
        return None
    return round(price * qty, 2)


def normalize_trade_metadata(source: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize any mode's payload into the canonical contract dict.

    Accepts a paper signal, a live contract/order payload, a backtest
    ``BacktestTrade.to_dict()`` row, or a raw trades-table row. Output has
    every canonical key; values that genuinely do not exist are None —
    never guessed, never defaulted to invented numbers.
    """
    src = dict(source or {})

    underlying = _first(src, *_ALIASES["underlying_symbol"])
    option_type = _as_text(_first(src, *_ALIASES["option_type"]))
    if option_type is not None:
        option_type = option_type.upper()
        if option_type not in ("CE", "PE"):
            option_type = None  # never persist an unvalidated option kind

    strike_f = _as_float(_first(src, *_ALIASES["strike_price"]))

    quantity = _as_int(_first(src, "quantity", "executed_quantity", "filled_qty"))
    entry_price = _as_float(_first(src, "entry_price", "fill_price", "premium", "price", "average_price"))
    capital = src.get("capital_used")
    if capital is None:
        capital = compute_capital_used(entry_price, quantity)
    else:
        # Recompute from executed fill data; if the fill data is invalid the
        # stored value is dropped (never trust an unverifiable capital).
        recomputed = compute_capital_used(entry_price, quantity)
        capital = recomputed if recomputed is not None else None

    lot_size = _as_int(_first(src, *_ALIASES["lot_size"]))
    if lot_size is not None and lot_size <= 0:
        lot_size = None

    out: Dict[str, Any] = {
        "underlying_symbol": _as_text(underlying),
        "option_type": option_type,
        "strike_price": strike_f,
        "expiry": _as_text(_first(src, *_ALIASES["expiry"])),
        "instrument_key": _as_text(_first(src, *_ALIASES["instrument_key"])),
        "entry_price": entry_price,
        "exit_price": _as_float(src.get("exit_price")),
        "quantity": quantity,
        "lot_size": lot_size,
        "capital_used": capital,
        "entry_timestamp": _as_text(_first(src, "entry_timestamp", "entry_time", "timestamp")),
        "exit_timestamp": _as_text(_first(src, "exit_timestamp", "exit_time")),
        "strategy": _as_text(_first(src, "strategy", "strategy_name")),
        "status": _as_text(src.get("status")) or "open",
        # internal bookkeeping (not part of the public contract columns but
        # persisted in the same row: signal_id/order_id go into notes/order_id)
        "trade_id": _as_text(src.get("trade_id")),
        "order_id": _as_text(src.get("order_id")),
        "signal_id": _as_text(src.get("signal_id")),
    }
    return out


def metadata_for_trade_row(normalized: Dict[str, Any]) -> Dict[str, Any]:
    """Canonical metadata keys that map 1:1 to trades-table columns."""
    return {k: normalized.get(k) for k in TRADE_METADATA_FIELDS}


def historical_display_value(value: Any) -> Any:
    """UI/API helper contract: absent historical metadata renders as the
    documented N/A note — callers must never invent a substitute value."""
    if value is None or value == "":
        return _HISTORICAL_NOTE
    return value
