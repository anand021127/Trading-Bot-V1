"""Copilot API routes.

Deliberately does NOT include any endpoint that places/modifies/cancels
an order. This session's Copilot is architecture-first: it can analyze,
explain, and produce shadow-mode TradePlans that pass through the same
deterministic validation a live plan would need to pass, but wiring an
approved TradePlan into the existing OrderManager for actual (paper or
live) execution is explicitly OUT of scope for this pass — see
docs/COPILOT.md "What remains before LIVE trading."
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from backend.copilot.config import load_copilot_settings
from backend.copilot.conversational import chat as copilot_chat
from backend.copilot.tools import CopilotTools

router = APIRouter()


class ChatRequest(BaseModel):
    question: str
    symbols: Optional[List[str]] = None  # which symbols to pull candles for, if the caller has them cached


class TradePlanRequest(BaseModel):
    symbol: str


def _build_tools(request: Request) -> CopilotTools:
    state = request.app.state
    return CopilotTools(
        engine=getattr(state, "engine", None),
        health_monitor=getattr(state, "health_monitor", None),
        scanner=getattr(state, "scanner", None),
        ws_client=getattr(state, "ws_client", None),
    )


@router.get("/status")
def copilot_status() -> Dict[str, Any]:
    settings = load_copilot_settings()
    return {
        "enabled": settings.enabled,
        "mode": settings.mode,
        "llm_backend": settings.llm_backend,
        "min_risk_reward": settings.min_risk_reward,
        "max_quote_age_seconds": settings.max_quote_age_seconds,
    }


@router.post("/chat")
def copilot_chat_endpoint(body: ChatRequest, request: Request) -> Dict[str, Any]:
    settings = load_copilot_settings()
    if not settings.enabled:
        return {"question": body.question,
                "answer": "The Copilot is disabled (COPILOT_ENABLED=false). Diagnostics/status endpoints still work.",
                "resolved_context": {}, "adapter": None}
    tools = _build_tools(request)
    # Live candles are fetched internally by conversational.py's plan
    # functions via tools.get_live_candles() when not supplied here — an
    # explicit candles_by_symbol is only needed if the caller has a
    # fresher in-memory window than a fresh REST fetch would give.
    return copilot_chat(body.question, tools, candles_by_symbol={})


@router.get("/diagnostics")
def copilot_diagnostics(request: Request) -> Dict[str, Any]:
    tools = _build_tools(request)
    return tools.run_full_diagnostics()


@router.post("/trade-plan")
def copilot_trade_plan(body: TradePlanRequest, request: Request) -> Dict[str, Any]:
    settings = load_copilot_settings()
    if not settings.enabled:
        return {"available": False, "reason": "The Copilot is disabled (COPILOT_ENABLED=false)."}
    tools = _build_tools(request)
    return tools.get_trade_plan(body.symbol)


@router.get("/positions")
def copilot_positions(request: Request) -> Dict[str, Any]:
    tools = _build_tools(request)
    return {"open_positions": tools.get_open_positions(), "account_risk": tools.get_account_risk()}


@router.get("/pnl")
def copilot_pnl(request: Request) -> Dict[str, Any]:
    tools = _build_tools(request)
    return tools.get_daily_pnl()


@router.get("/shadow-performance")
def copilot_shadow_performance() -> Dict[str, Any]:
    from backend.copilot.performance_report import build_shadow_performance_report
    return build_shadow_performance_report()
