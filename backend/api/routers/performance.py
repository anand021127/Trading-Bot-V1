"""Router for performance snapshots and analytics."""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter

from backend.database.db_manager import DatabaseManager
from backend.config.settings import load_settings

router = APIRouter()
settings = load_settings()

db_manager = DatabaseManager(db_path=settings.database.path)


@router.get("/")
async def get_performance() -> Dict[str, Any]:
    snapshots = []
    try:
        rows = db_manager.list_performance_snapshots()
        snapshots = [snapshot.__dict__ for snapshot in rows]
    except AttributeError:
        snapshots = []

    return {
        "performance": snapshots,
        "total_snapshots": len(snapshots),
    }
