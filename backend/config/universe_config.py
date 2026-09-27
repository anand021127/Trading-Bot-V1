"""Index-options universe configuration."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

MODE_OPTIONS = "OPTIONS"
VALID_MODES = (MODE_OPTIONS,)
# The SIX supported index option underlyings (PHASE 5.3 §2 adds BANKEX).
# Everything index-specific (exchange segment, strike step, static fallback
# instrument key) is derived from the broker metadata at runtime; this list
# is only the whitelist of what the bot may trade at all.
VALID_OPTION_INDICES = ["NIFTY50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"]

# Exchange segment per underlying (informational + tests; the live instrument
# key ALWAYS comes from the daily-refreshed Upstox instrument master — never
# from this table).
INDEX_EXCHANGE = {
    "NIFTY50": "NSE",
    "BANKNIFTY": "NSE",
    "FINNIFTY": "NSE",
    "MIDCPNIFTY": "NSE",
    "SENSEX": "BSE",
    "BANKEX": "BSE",
}

# Nominal ATM strike steps, used ONLY as a plausibility cross-check in the
# contract validator (a resolved ATM strike far off-grid signals a bad
# chain row). The broker chain response is authoritative for what strikes
# exist; exchange strike schemes do change, so the validator warns via
# rejection only for the specific index it knows, and never for unknown ones.
INDEX_STRIKE_STEP = {
    "NIFTY50": 50,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
    "MIDCPNIFTY": 75,
    "SENSEX": 100,
    "BANKEX": 100,
}

_UNIVERSE_KEY = "universe_config_json"


@dataclass
class UniverseConfig:
    mode: str = MODE_OPTIONS
    option_indices: List[str] = field(default_factory=lambda: ["NIFTY50"])

    def resolve_symbols(self) -> List[str]:
        return [s for s in self.option_indices if s in VALID_OPTION_INDICES]

    def validate(self) -> Optional[str]:
        unknown = [s for s in self.option_indices if s not in VALID_OPTION_INDICES]
        if unknown:
            return f"Unknown option indices: {unknown}"
        if self.mode != MODE_OPTIONS:
            return f"Unsupported mode: {self.mode}"
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {"mode": self.mode, "option_indices": list(self.option_indices)}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UniverseConfig":
        mode = str(data.get("mode") or MODE_OPTIONS)
        if mode == "NIFTY_OPTIONS":
            return cls(mode=MODE_OPTIONS, option_indices=["NIFTY50"])
        if mode == "BANKNIFTY_OPTIONS":
            return cls(mode=MODE_OPTIONS, option_indices=["BANKNIFTY"])
        if mode not in VALID_MODES:
            return cls(mode=MODE_OPTIONS, option_indices=["NIFTY50"])
        indices = data.get("option_indices") or ["NIFTY50"]
        return cls(mode=MODE_OPTIONS, option_indices=list(indices))


def save_universe_config(database: Any, config: UniverseConfig) -> None:
    import json
    database.save_setting(_UNIVERSE_KEY, json.dumps(config.to_dict()))


def load_universe_config(database: Any) -> UniverseConfig:
    import json
    raw = database.get_setting(_UNIVERSE_KEY, "")
    if not raw:
        return UniverseConfig()
    try:
        return UniverseConfig.from_dict(json.loads(raw))
    except Exception:
        return UniverseConfig()
