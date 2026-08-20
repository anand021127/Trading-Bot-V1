"""Live Scanner API — item #3.

Exposes exactly what the scanner is currently analyzing, per-symbol
indicator status, and the plain-English decision for every instrument in
the configured universe.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

router = APIRouter()

# Shared reference — set by main.py at startup (same pattern as bot_control).
_scanner_ref: Any = None


def set_scanner(scanner: Any) -> None:
    global _scanner_ref
    _scanner_ref = scanner


@router.get("/status")
async def get_scanner_status() -> Dict[str, Any]:
    if _scanner_ref is None:
        raise HTTPException(status_code=503, detail="Scanner not initialized")
    return _scanner_ref.status_report()


@router.get("/symbol/{symbol}")
async def get_scanner_symbol(symbol: str) -> Dict[str, Any]:
    if _scanner_ref is None:
        raise HTTPException(status_code=503, detail="Scanner not initialized")
    result = _scanner_ref.get_result(symbol.upper())
    if result is None:
        raise HTTPException(status_code=404, detail=f"No scan result yet for {symbol.upper()} — it may not be in the current universe, or hasn't been scanned yet")
    return result.to_dict()


@router.post("/scan-now")
async def trigger_scan_now() -> Dict[str, Any]:
    """Force one synchronous full pass right now (useful right after
    changing the universe, instead of waiting for the background loop)."""
    if _scanner_ref is None:
        raise HTTPException(status_code=503, detail="Scanner not initialized")
    results = _scanner_ref.scan_once()
    return {"scanned": len(results), "results": [r.to_dict() for r in results]}
