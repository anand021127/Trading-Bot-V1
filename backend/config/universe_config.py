"""Trading universe configuration.

Controls WHICH instruments the bot (and the live scanner) actually looks
at. Persisted via the same SQLite key-value store used for settings/tokens
so it survives restarts on Render.

Modes:
  STOCKS            — scan individual NIFTY50 (or custom) equity symbols.
  NIFTY_OPTIONS     — scan NIFTY 50 index + trade its option premiums.
  BANKNIFTY_OPTIONS — scan BANKNIFTY index + trade its option premiums.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

MODE_STOCKS = "STOCKS"
MODE_NIFTY_OPTIONS = "NIFTY_OPTIONS"
MODE_BANKNIFTY_OPTIONS = "BANKNIFTY_OPTIONS"
VALID_MODES = (MODE_STOCKS, MODE_NIFTY_OPTIONS, MODE_BANKNIFTY_OPTIONS)

INDEX_NIFTY50 = "NIFTY50"
INDEX_CUSTOM = "CUSTOM"

_DB_KEY = "trading_universe_config"

# Kept here (rather than importing from trading_engine, which would create a
# circular import) — canonical NIFTY50 constituent list used whenever the
# selected index is "NIFTY50" and mode is STOCKS.
NIFTY50_SYMBOLS: List[str] = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR", "KOTAKBANK",
    "LT", "SBIN", "AXISBANK", "BHARTIARTL", "ITC", "ASIANPAINT", "MARUTI", "HCLTECH",
    "SUNPHARMA", "WIPRO", "TITAN", "ULTRACEMCO", "BAJFINANCE", "NESTLEIND", "ADANIENT",
    "TATAMOTORS", "TATASTEEL", "POWERGRID", "NTPC", "M&M", "BAJAJFINSV", "HDFCLIFE",
    "SBILIFE", "GRASIM", "JSWSTEEL", "TECHM", "INDUSINDBK", "ADANIPORTS", "CIPLA",
    "DRREDDY", "EICHERMOT", "COALINDIA", "HEROMOTOCO", "BRITANNIA", "DIVISLAB",
    "APOLLOHOSP", "BPCL", "ONGC", "TATACONSUM", "UPL", "SHREECEM", "HINDALCO",
    "BAJAJ-AUTO", "VEDL",
]


@dataclass
class UniverseConfig:
    mode: str = MODE_STOCKS
    index: str = INDEX_NIFTY50          # NIFTY50 | CUSTOM (ignored for options modes)
    custom_symbols: List[str] = field(default_factory=list)
    max_symbols: int = 20               # cap how many the scanner watches at once

    def resolve_symbols(self) -> List[str]:
        """The actual list of instruments the scanner/bot should look at
        right now, given this config. Never silently expands beyond what
        the user picked."""
        if self.mode == MODE_NIFTY_OPTIONS:
            return ["NIFTY50"]
        if self.mode == MODE_BANKNIFTY_OPTIONS:
            return ["BANKNIFTY"]
        # STOCKS mode
        if self.index == INDEX_CUSTOM:
            return self.custom_symbols[: self.max_symbols]
        return NIFTY50_SYMBOLS[: self.max_symbols]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UniverseConfig":
        return cls(
            mode=data.get("mode", MODE_STOCKS),
            index=data.get("index", INDEX_NIFTY50),
            custom_symbols=list(data.get("custom_symbols", [])),
            max_symbols=int(data.get("max_symbols", 20)),
        )

    def validate(self) -> Optional[str]:
        """Return an error string if invalid, else None."""
        if self.mode not in VALID_MODES:
            return f"Invalid mode '{self.mode}'. Must be one of {VALID_MODES}."
        if self.index not in (INDEX_NIFTY50, INDEX_CUSTOM):
            return f"Invalid index '{self.index}'. Must be NIFTY50 or CUSTOM."
        if self.index == INDEX_CUSTOM and self.mode == MODE_STOCKS and not self.custom_symbols:
            return "index=CUSTOM requires at least one symbol in custom_symbols."
        if self.max_symbols <= 0:
            return "max_symbols must be positive."
        return None


def load_universe_config(db: Any) -> UniverseConfig:
    raw = db.get_setting(_DB_KEY, "")
    if not raw:
        return UniverseConfig()
    try:
        return UniverseConfig.from_dict(json.loads(raw))
    except Exception:
        return UniverseConfig()


def save_universe_config(db: Any, config: UniverseConfig) -> None:
    db.save_setting(_DB_KEY, json.dumps(config.to_dict()))
