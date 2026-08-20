"""Trading universe configuration.

Controls the index underlyings whose option chains and premiums the bot may
trade. The universe is intentionally options-only; individual equities are
not valid instruments for this application.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

MODE_OPTIONS = "OPTIONS"
VALID_MODES = (MODE_OPTIONS,)
VALID_OPTION_INDICES = (
    "NIFTY50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX",
)

# Legacy option-mode names are normalized during migration for existing deployments.
_LEGACY_MODE_NIFTY_OPTIONS = "NIFTY_OPTIONS"
_LEGACY_MODE_BANKNIFTY_OPTIONS = "BANKNIFTY_OPTIONS"

_DB_KEY = "trading_universe_config"

@dataclass
class UniverseConfig:
    mode: str = MODE_OPTIONS
    option_indices: List[str] = field(default_factory=lambda: ["NIFTY50"])

    def resolve_symbols(self) -> List[str]:
        """The actual list of instruments the scanner/bot should look at
        right now. For OPTIONS mode this is the underlying indices whose
        option premiums get traded (via OptionPremiumStrategy) — NOT the
        indices' own price action."""
        return [i for i in self.option_indices if i in VALID_OPTION_INDICES]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UniverseConfig":
        mode = data.get("mode", MODE_OPTIONS)
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
        elif mode != MODE_OPTIONS:
            mode = MODE_OPTIONS
            option_indices = option_indices or ["NIFTY50"]

        return cls(
            mode=mode,
            option_indices=option_indices or ["NIFTY50"],
        )

    def validate(self) -> Optional[str]:
        """Return an error string if invalid, else None."""
        if self.mode != MODE_OPTIONS:
            return f"Invalid mode '{self.mode}'. Options mode is the only supported mode."
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
