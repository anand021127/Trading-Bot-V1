"""Index-options universe configuration."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

MODE_OPTIONS = "OPTIONS"
VALID_MODES = (MODE_OPTIONS,)
VALID_OPTION_INDICES = ["NIFTY50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]

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
