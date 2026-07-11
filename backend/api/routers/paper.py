"""Paper trading status API — backs the frontend's Paper Trading page.

`/status` — real readiness checklist computed from the actual trade log
(never fabricated). `/positions` is a thin alias for the live-positions
detail (item #7 fields), kept here too since it's the natural place the
paper-trading UI looks for it.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter

from backend.paper.status_calculator import compute_paper_status

router = APIRouter()


@router.get("/status")
async def get_paper_status() -> Dict[str, Any]:
    return compute_paper_status()


@router.get("/positions")
async def get_paper_positions() -> Dict[str, Any]:
    import backend.api.routers.bot_control as bot_control_module
    engine = bot_control_module._engine_ref
    if engine is None:
        return {"positions": [], "note": "Trading engine not initialized"}
    try:
        return {"positions": engine.get_open_positions_detail()}
    except Exception as e:
        return {"positions": [], "error": str(e)}
