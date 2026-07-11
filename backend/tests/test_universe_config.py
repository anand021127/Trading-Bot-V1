"""Tests for trading universe selection (item #4)."""
from __future__ import annotations

import tempfile
import uuid

from backend.config.universe_config import (
    UniverseConfig,
    MODE_STOCKS,
    MODE_NIFTY_OPTIONS,
    MODE_BANKNIFTY_OPTIONS,
    INDEX_CUSTOM,
    INDEX_NIFTY50,
    NIFTY50_SYMBOLS,
    load_universe_config,
    save_universe_config,
)
from backend.database.db_manager import DatabaseManager


def _memory_db() -> DatabaseManager:
    # NOTE: DatabaseManager's ":memory:" mode uses SQLite shared-cache, which
    # is shared across ALL ":memory:" instances in the same process — so we
    # use a unique temp file per test instead to keep them isolated.
    path = f"{tempfile.gettempdir()}/test_universe_{uuid.uuid4().hex}.db"
    db = DatabaseManager(db_path=path)
    db.init_db()
    return db


def test_default_config_is_nifty50_stocks() -> None:
    config = UniverseConfig()
    assert config.mode == MODE_STOCKS
    assert config.index == INDEX_NIFTY50
    symbols = config.resolve_symbols()
    assert symbols[0] == NIFTY50_SYMBOLS[0]
    assert len(symbols) == config.max_symbols


def test_custom_symbols_used_when_index_is_custom() -> None:
    config = UniverseConfig(mode=MODE_STOCKS, index=INDEX_CUSTOM, custom_symbols=["RELIANCE", "TCS"])
    assert config.resolve_symbols() == ["RELIANCE", "TCS"]


def test_nifty_options_mode_resolves_to_index_only() -> None:
    config = UniverseConfig(mode=MODE_NIFTY_OPTIONS)
    assert config.resolve_symbols() == ["NIFTY50"]


def test_banknifty_options_mode_resolves_to_index_only() -> None:
    config = UniverseConfig(mode=MODE_BANKNIFTY_OPTIONS)
    assert config.resolve_symbols() == ["BANKNIFTY"]


def test_max_symbols_caps_the_watchlist() -> None:
    config = UniverseConfig(max_symbols=5)
    assert len(config.resolve_symbols()) == 5


def test_validate_rejects_unknown_mode() -> None:
    config = UniverseConfig(mode="NOT_A_MODE")
    assert config.validate() is not None


def test_validate_rejects_custom_index_without_symbols() -> None:
    config = UniverseConfig(mode=MODE_STOCKS, index=INDEX_CUSTOM, custom_symbols=[])
    assert config.validate() is not None


def test_validate_accepts_options_mode_without_custom_symbols() -> None:
    config = UniverseConfig(mode=MODE_NIFTY_OPTIONS, index=INDEX_CUSTOM, custom_symbols=[])
    assert config.validate() is None


def test_save_and_load_roundtrip() -> None:
    db = _memory_db()
    config = UniverseConfig(mode=MODE_BANKNIFTY_OPTIONS, max_symbols=10)
    save_universe_config(db, config)
    loaded = load_universe_config(db)
    assert loaded.mode == MODE_BANKNIFTY_OPTIONS
    assert loaded.max_symbols == 10


def test_load_returns_default_when_nothing_saved() -> None:
    db = _memory_db()
    loaded = load_universe_config(db)
    assert loaded.mode == MODE_STOCKS
