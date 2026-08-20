"""Tests for the index-options-only trading universe."""
from __future__ import annotations

import tempfile
import uuid

from backend.config.universe_config import (
    MODE_OPTIONS,
    VALID_OPTION_INDICES,
    UniverseConfig,
    load_universe_config,
    save_universe_config,
)
from backend.database.db_manager import DatabaseManager


def _db() -> DatabaseManager:
    path = f"{tempfile.gettempdir()}/test_universe_{uuid.uuid4().hex}.db"
    database = DatabaseManager(db_path=path)
    database.init_db()
    return database


def test_default_config_is_options_only() -> None:
    config = UniverseConfig()
    assert config.mode == MODE_OPTIONS
    assert config.resolve_symbols() == ["NIFTY50"]


def test_all_supported_indices_are_valid() -> None:
    config = UniverseConfig(option_indices=list(VALID_OPTION_INDICES))
    assert config.validate() is None
    assert config.resolve_symbols() == list(VALID_OPTION_INDICES)


def test_invalid_index_is_rejected() -> None:
    config = UniverseConfig(option_indices=["NIFTY50", "UNKNOWN"])
    assert config.resolve_symbols() == ["NIFTY50"]
    assert config.validate() is not None


def test_stock_mode_migrates_to_nifty_options() -> None:
    config = UniverseConfig.from_dict({"mode": "UNKNOWN_MODE"})
    assert config.mode == MODE_OPTIONS
    assert config.option_indices == ["NIFTY50"]


def test_legacy_option_modes_migrate() -> None:
    assert UniverseConfig.from_dict({"mode": "NIFTY_OPTIONS"}).option_indices == ["NIFTY50"]
    assert UniverseConfig.from_dict({"mode": "BANKNIFTY_OPTIONS"}).option_indices == ["BANKNIFTY"]


def test_save_and_load_roundtrip() -> None:
    database = _db()
    save_universe_config(database, UniverseConfig(option_indices=["NIFTY50", "SENSEX"]))
    loaded = load_universe_config(database)
    assert loaded.mode == MODE_OPTIONS
    assert loaded.option_indices == ["NIFTY50", "SENSEX"]
