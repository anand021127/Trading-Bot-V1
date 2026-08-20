"""Helpers for live market data feed normalization.

The module provides a simple adapter that transforms a broker-style quote payload
into a compact dictionary that can be consumed by the UI or strategy modules.
"""

from __future__ import annotations

from typing import Any, Dict


def normalize_quote(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the latest quote details from a raw live-feed payload."""
    quote = payload.get("data", {})
    return {
        "symbol": quote.get("symbol"),
        "last_price": quote.get("last_price"),
        "change": quote.get("change"),
    }
