"""Copilot API routes.

Deliberately does NOT include any endpoint that places/modifies/cancels
an order. The Copilot is an OBSERVATION/EXPLANATION assistant: it can
analyze, explain, and describe bot/trade/backtest state, but it cannot
switch modes, place orders, change risk/strategy/credentials, or delete
anything.

Chat is now ASYNC (mirrors the backtest job pattern) so an AI response
can take longer than any frontend HTTP timeout without the UI failing:

  POST /api/copilot/chat/submit          -> 202 {job_id, status} immediately
  GET  /api/copilot/chat/status/{job_id} -> queued/thinking/completed/failed/cancelled
  POST /api/copilot/chat/status/{job_id}/cancel -> cooperative cancel

POST /api/copilot/chat (legacy) now returns the same job envelope when a
provider is configured; when NO provider is configured it returns an
explicit configuration message instead of a canned fake answer.

Provider failures are TYPED (see provider_errors.py): provider
unreachable, provider timeout, auth failure, rate limit, model
unavailable, backend exception, user cancellation — the frontend never
collapses them into a generic "timeout of 30000ms exceeded".

Context grounding: every job first resolves project context
deterministically (conversational intent routing + the BotContext /
TradeContext / BacktestContext layer in context.py), redacts secrets
(secret_guard.py), and only then hands the context to the provider.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.copilot.config import load_copilot_settings
from backend.copilot.conversation_state import get_session
from backend.copilot.tools import CopilotTools

router = APIRouter()


class ChatRequest(BaseModel):
    question: str
    symbols: Optional[List[str]] = None  # reserved: cached candles per symbol
    session_id: Optional[str] = None  # carries conversation memory across turns


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


def _resolve_context(question: str, tools: CopilotTools, state: Any) -> Dict[str, Any]:
    """Deterministic, question-relevant project context for ONE chat turn.

    Combines the existing intent router's tool resolution (market status,
    analysis, trade plan, diagnostics — all real tool outputs, never LLM
    guesses) with the BotContext/TradeContext/BacktestContext layer, then
    redacts secrets. Never dumps the database or log files wholesale.
    """
    from backend.copilot.context import build_context
    from backend.copilot.conversational import (
        INTENT_DIAGNOSTICS,
        INTENT_MARKET,
        INTENT_TRADING,
        route_question,
    )

    plan_fn = route_question(question)
    base_ctx = plan_fn(tools, question, {}, state)
    intent = base_ctx.get("_intent") or ""

    extra = build_context(
        question,
        tools,
        include_bot=intent in (INTENT_DIAGNOSTICS, INTENT_MARKET),
        include_trades=intent == INTENT_TRADING,
    )

    merged: Dict[str, Any] = dict(extra)
    for k, v in base_ctx.items():
        if not k.startswith("_"):
            merged[k] = v
    merged["_intent"] = intent
    if base_ctx.get("_symbol"):
        merged["_symbol"] = base_ctx["_symbol"]
    return merged


@router.get("/status")
def copilot_status() -> Dict[str, Any]:
    settings = load_copilot_settings()
    provider_configured = settings.llm_backend != "none"
    if settings.llm_backend == "openai" and not settings.ai_api_key:
        provider_configured = False
    return {
        "enabled": settings.enabled,
        "mode": settings.mode,
        "llm_backend": settings.llm_backend,
        "provider_configured": provider_configured,
        "model": settings.llm_model,
        "min_risk_reward": settings.min_risk_reward,
        "max_quote_age_seconds": settings.max_quote_age_seconds,
        # Deliberately NO key material, URLs with tokens, or env values here.
    }


@router.post("/chat/submit")
def copilot_chat_submit(body: ChatRequest, request: Request) -> JSONResponse:
    """Start an async chat job. Returns 202 with a job id IMMEDIATELY —
    the HTTP request never stays open while the provider thinks."""
    settings = load_copilot_settings()
    if not settings.enabled:
        return JSONResponse(status_code=200, content={
            "job_id": None,
            "status": "failed",
            "error_code": "COPILOT_DISABLED",
            "error": "The Copilot is disabled (COPILOT_ENABLED=false).",
            "session_id": body.session_id,
        })
    if settings.llm_backend == "none" or (
        settings.llm_backend == "openai" and not settings.ai_api_key
    ):
        return JSONResponse(status_code=200, content={
            "job_id": None,
            "status": "failed",
            "error_code": "PROVIDER_NOT_CONFIGURED",
            "error": (
                "No AI provider is configured. Set COPILOT_LLM_BACKEND to "
                "'local_openai_compatible' (Ollama) or 'openai' with its API "
                "key env var, then retry. No fake answer was generated."
            ),
            "session_id": body.session_id,
        })

    from backend.copilot.chat_jobs import chat_job_manager

    provider_configured = not (
        settings.llm_backend == "none"
        or (settings.llm_backend == "openai" and not settings.ai_api_key)
    )
    session_id = body.session_id or str(uuid.uuid4())
    state = get_session(session_id)
    tools = _build_tools(request)
    resolved_context = _resolve_context(body.question, tools, state)
    history = state.recent_history()
    job = chat_job_manager.submit(
        body.question, resolved_context, history,
        state=state, provider_configured=provider_configured,
    )
    return JSONResponse(
        status_code=202,
        content={"job_id": job.job_id, "status": job.status, "session_id": session_id},
    )


@router.get("/chat/status/{job_id}")
def copilot_chat_status(job_id: str, request: Request) -> Dict[str, Any]:
    from backend.copilot.chat_jobs import chat_job_manager

    job = chat_job_manager.get(job_id)
    if job is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"No chat job found with id {job_id}")
    return job.to_dict()


@router.post("/chat/status/{job_id}/cancel")
def copilot_chat_cancel(job_id: str, request: Request) -> Dict[str, Any]:
    from backend.copilot.chat_jobs import chat_job_manager

    job = chat_job_manager.cancel(job_id)
    if job is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"No chat job found with id {job_id}")
    return {"job_id": job_id, "status": job.status, "cancelled": True}


@router.post("/chat")
def copilot_chat_endpoint(body: ChatRequest, request: Request) -> Dict[str, Any]:
    """Legacy sync endpoint, kept for compatibility.

    With a provider configured it now delegates to the async job manager
    and returns the job envelope (job_id + status) — callers poll
    /chat/status/{job_id}. With no provider configured it returns an
    explicit configuration message; it NEVER returns a canned answer
    pretending the AI understood the question.
    """
    settings = load_copilot_settings()
    session_id = body.session_id or str(uuid.uuid4())
    if not settings.enabled:
        return {"question": body.question,
                "answer": "The Copilot is disabled (COPILOT_ENABLED=false). Diagnostics/status endpoints still work.",
                "resolved_context": {}, "adapter": None, "session_id": session_id}
    if settings.llm_backend == "none" or (
        settings.llm_backend == "openai" and not settings.ai_api_key
    ):
        return {
            "question": body.question,
            "answer": (
                "No AI provider is configured (COPILOT_LLM_BACKEND=none). "
                "Configure a provider in the backend environment to enable "
                "answers; no fabricated response was generated."
            ),
            "resolved_context": {},
            "adapter": None,
            "error_code": "PROVIDER_NOT_CONFIGURED",
            "session_id": session_id,
        }

    from backend.copilot.chat_jobs import chat_job_manager

    provider_configured = not (
        settings.llm_backend == "none"
        or (settings.llm_backend == "openai" and not settings.ai_api_key)
    )
    state = get_session(session_id)
    tools = _build_tools(request)
    resolved_context = _resolve_context(body.question, tools, state)
    history = state.recent_history()
    job = chat_job_manager.submit(
        body.question, resolved_context, history,
        state=state, provider_configured=provider_configured,
    )
    return {
        "question": body.question,
        "job_id": job.job_id,
        "status": job.status,
        "resolved_context": resolved_context,
        "adapter": "async_job",
        "session_id": session_id,
    }


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
