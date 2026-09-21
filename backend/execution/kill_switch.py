"""Persistent emergency-stop states."""
from __future__ import annotations

from enum import Enum
from typing import Any

STOP_NEW_ENTRIES = "STOP_NEW_ENTRIES"
CLOSE_EXISTING_POSITIONS = "CLOSE_EXISTING_POSITIONS"
FULL_SYSTEM_STOP = "FULL_SYSTEM_STOP"


class KillLevel(str, Enum):
    OFF = "OFF"
    STOP_NEW_ENTRIES = STOP_NEW_ENTRIES
    CLOSE_EXISTING_POSITIONS = CLOSE_EXISTING_POSITIONS
    FULL_SYSTEM_STOP = FULL_SYSTEM_STOP


_KEY = "persistent_kill_level"


class PersistentKillSwitch:
    def __init__(self, db: Any) -> None:
        self.db = db

    def set_level(self, level: str, reason: str = "") -> None:
        if level not in {e.value for e in KillLevel}:
            raise ValueError(level)
        self.db.save_setting(_KEY, level)
        self.db.save_setting(_KEY + "_reason", reason)

    def level(self) -> str:
        return self.db.get_setting(_KEY, KillLevel.OFF.value) or KillLevel.OFF.value

    def blocks_entries(self) -> bool:
        return self.level() != KillLevel.OFF.value

    def requires_flatten(self) -> bool:
        return self.level() in (KillLevel.CLOSE_EXISTING_POSITIONS.value, KillLevel.FULL_SYSTEM_STOP.value)
