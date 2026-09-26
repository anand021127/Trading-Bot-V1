"""Paper trading status API — backs the frontend's Paper Trading page.

`/status` — real readiness checklist computed from the actual trade log
(never fabricated). `/positions` returns the worker's authoritative
PaperBroker book (via the durable ledger) — never a fabricated empty list.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter

from backend.paper.status_calculator import compute_paper_status

from fastapi import Depends

from backend.api.control_auth import require_control_token

# State-changing endpoints: guarded by the optional control token
# (no-op unless CONTROL_TOKEN is set).
router = APIRouter(dependencies=[Depends(require_control_token)])


def _paper_runtime() -> Any:
    import backend.api.routers.bot_control as bot_control_module
    runtime = bot_control_module.get_paper_runtime()
    if runtime is not None:
        return runtime
    try:
        import backend.api.main as main_mod
        state = getattr(main_mod, "app", None)
        state = getattr(state, "state", None) if state is not None else None
        return getattr(state, "paper_runtime", None)
    except Exception:
        return None


@router.get("/status")
async def get_paper_status() -> Dict[str, Any]:
    return compute_paper_status()


@router.get("/positions")
async def get_paper_positions() -> Dict[str, Any]:
    """Open paper positions from the durable SQLite ledger (instrument_key,
    qty, avg price, entry time, SL/target/lot from the persisted extra
    state). The worker's PaperBroker is hydrated from this same ledger, so
    this reflects what the trading runtime actually holds after any restart.
    A missing runtime is reported honestly instead of an empty list."""
    runtime = _paper_runtime()
    if runtime is None:
        return {
            "positions": [],
            "paper_runtime_attached": False,
            "note": "PaperTradingRuntime not initialized — positions unavailable",
        }
    try:
        positions = []
        for pos in runtime.db.get_open_positions():
            ik = getattr(pos, "instrument_key", None) or pos.symbol or ""
            extra = getattr(pos, "extra", None) or {}
            qty = int(pos.quantity or 0)
            avg_px = float(pos.average_price or 0)
            # Capital actually deployed in this paper position = entry price ×
            # executed quantity (the common trade metadata definition — never
            # allocation or account capital).
            capital_used = round(avg_px * qty, 2) if avg_px > 0 and qty > 0 else None
            positions.append({
                "instrument_key": ik,
                "symbol": ik,
                "quantity": qty,
                "average_price": avg_px,
                "entry_price": avg_px,
                "entry_time": pos.entry_time.isoformat() if hasattr(pos.entry_time, "isoformat") else str(pos.entry_time),
                "capital_used": capital_used,
                "stop_loss": extra.get("stop_loss"),
                "target": extra.get("target"),
                "lot_size": extra.get("lot_size"),
                "underlying": extra.get("underlying", ""),
                "underlying_symbol": extra.get("underlying", ""),
                "option_type": extra.get("option_type", ""),
                "strike": extra.get("strike"),
                "strike_price": extra.get("strike"),
                "expiry": extra.get("expiry", ""),
                "trade_id": extra.get("trade_id", ""),
                "strategy": extra.get("strategy", ""),
                "status": "open",
            })
        return {"positions": positions, "paper_runtime_attached": True}
    except Exception as e:
        return {"positions": [], "paper_runtime_attached": False, "error": str(e)}
