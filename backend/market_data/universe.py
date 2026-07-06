"""Helpers for building the trading universe of symbols.

This lightweight module selects instruments that are currently active so later
market-data and strategy modules can focus on tradable assets.
"""

from __future__ import annotations

from typing import Any, Dict, List


def build_universe(instruments: List[Dict[str, Any]]) -> List[str]:
    """Return the list of active instrument symbols from a raw instrument list."""
    return [instrument["symbol"] for instrument in instruments if instrument.get("active", False)]
