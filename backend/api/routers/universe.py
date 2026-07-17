"""Trading universe selection API — item #4.

The bot and the live scanner only ever look at what's configured here.
Changing this does NOT require a restart — it's read fresh on every scan
cycle.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from backend.config.settings import load_settings
from backend.config.universe_config import (
    UniverseConfig,
    load_universe_config,
    save_universe_config,
    NIFTY50_SYMBOLS,
    VALID_MODES,
    VALID_OPTION_INDICES,
)
from backend.database.db_manager import DatabaseManager

router = APIRouter()
_settings = load_settings()
_db = DatabaseManager(db_path=_settings.database.path)


@router.get("/")
async def get_universe() -> Dict[str, Any]:
    config = load_universe_config(_db)
    return {
        **config.to_dict(),
        "resolved_symbols": config.resolve_symbols(),
        "valid_modes": list(VALID_MODES),
        "valid_option_indices": list(VALID_OPTION_INDICES),
        "nifty50_constituents": NIFTY50_SYMBOLS,
    }


@router.put("/")
async def update_universe(body: Dict[str, Any]) -> Dict[str, Any]:
    current = load_universe_config(_db)
    merged = UniverseConfig(
        mode=body.get("mode", current.mode),
        index=body.get("index", current.index),
        custom_symbols=body.get("custom_symbols", current.custom_symbols),
        max_symbols=body.get("max_symbols", current.max_symbols),
        option_indices=body.get("option_indices", current.option_indices),
    )
    error = merged.validate()
    if error:
        raise HTTPException(status_code=400, detail=error)

    save_universe_config(_db, merged)
    return {
        "saved": True,
        **merged.to_dict(),
        "resolved_symbols": merged.resolve_symbols(),
        # These three were missing from the PUT response while GET / had
        # them — the frontend's UniverseSection reads them unconditionally
        # on every render (e.g. `universe.valid_option_indices.map(...)`),
        # and replaces its whole state with whatever this endpoint returns
        # after a save. Omitting them here meant selecting OPTIONS mode
        # crashed the entire app with an uncaught "Cannot read properties
        # of undefined (reading 'map')" — no error boundary catches that,
        # so the page just went blank until a manual refresh re-fetched
        # the complete shape from GET.
        "valid_modes": list(VALID_MODES),
        "valid_option_indices": list(VALID_OPTION_INDICES),
        "nifty50_constituents": NIFTY50_SYMBOLS,
    }
