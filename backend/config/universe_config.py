"""Trading universe configuration.

Controls WHICH instruments the bot (and the live scanner) actually looks
at. Persisted via the same SQLite key-value store used for settings/tokens
so it survives restarts on Render.

Modes:
  STOCKS  — scan individual NIFTY50 (or custom) equity symbols using the
            EMA_TREND / ORB strategies on the stocks themselves.
  OPTIONS — scan one or more indices (NIFTY50, BANKNIFTY, SENSEX — any
            combination) and trade THEIR OPTION PREMIUMS via
            OptionPremiumStrategy (ATM strike detection, CE/PE selection,
            auto-selected nearest expiry) — not the index price itself.

`option_indices` lets you pick NIFTY + SENSEX together (or any subset) —
the old design only allowed one index at a time, which didn't match
"I built this for NIFTY and SENSEX options."
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

MODE_STOCKS = "STOCKS"
MODE_OPTIONS = "OPTIONS"
VALID_MODES = (MODE_STOCKS, MODE_OPTIONS)

INDEX_NIFTY50 = "NIFTY50"
INDEX_CUSTOM = "CUSTOM"

VALID_OPTION_INDICES = ("NIFTY50", "BANKNIFTY", "SENSEX")

# Legacy single-index modes from an earlier version — migrated automatically
# in from_dict() so previously-saved configs don't just silently break.
_LEGACY_MODE_NIFTY_OPTIONS = "NIFTY_OPTIONS"
_LEGACY_MODE_BANKNIFTY_OPTIONS = "BANKNIFTY_OPTIONS"

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
    index: str = INDEX_NIFTY50            # NIFTY50 | CUSTOM — only used when mode=STOCKS
    custom_symbols: List[str] = field(default_factory=list)
    max_symbols: int = 20                 # cap for STOCKS mode
    option_indices: List[str] = field(default_factory=lambda: ["NIFTY50"])  # OPTIONS mode

    def resolve_symbols(self) -> List[str]:
        """The actual list of instruments the scanner/bot should look at
        right now. For OPTIONS mode this is the underlying indices whose
        option premiums get traded (via OptionPremiumStrategy) — NOT the
        indices' own price action."""
        if self.mode == MODE_OPTIONS:
            return [i for i in self.option_indices if i in VALID_OPTION_INDICES]
        if self.index == INDEX_CUSTOM:
            return self.custom_symbols[: self.max_symbols]
        return NIFTY50_SYMBOLS[: self.max_symbols]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UniverseConfig":
        mode = data.get("mode", MODE_STOCKS)
        option_indices = list(data.get("option_indices", []))

        # Migrate configs saved under the old single-index mode names.
        if mode == _LEGACY_MODE_NIFTY_OPTIONS:
            mode = MODE_OPTIONS
            option_indices = option_indices or ["NIFTY50"]
        elif mode == _LEGACY_MODE_BANKNIFTY_OPTIONS:
            mode = MODE_OPTIONS
            option_indices = option_indices or ["BANKNIFTY"]
        elif mode == MODE_OPTIONS and not option_indices:
            option_indices = ["NIFTY50"]

        return cls(
            mode=mode,
            index=data.get("index", INDEX_NIFTY50),
            custom_symbols=list(data.get("custom_symbols", [])),
            max_symbols=int(data.get("max_symbols", 20)),
            option_indices=option_indices,
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
        if self.mode == MODE_OPTIONS:
            if not self.option_indices:
                return "OPTIONS mode requires at least one index in option_indices."
            bad = [i for i in self.option_indices if i not in VALID_OPTION_INDICES]
            if bad:
                return f"Invalid option_indices {bad}. Must be a subset of {VALID_OPTION_INDICES}."
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
