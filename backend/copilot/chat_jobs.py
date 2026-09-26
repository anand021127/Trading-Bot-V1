"""Asynchronous Copilot chat jobs.

Root cause addressed: POST /api/copilot/chat used to run the whole
tool-resolution + provider call synchronously inside one HTTP request.
A slow local LLM exceeded the frontend's axios window and the UI showed
"timeout of 30000ms exceeded" even though the backend was still working.

New flow (mirrors the backtest job pattern):
  POST /api/copilot/chat/submit  -> {job_id} immediately (202)
  GET  /api/copilot/chat/{job_id} -> status + incremental answer (short poll)
  POST /api/copilot/chat/{job_id}/cancel -> cooperative cancel

Every job resolves its project context FIRST (deterministic tools —
never the LLM), then asks the configured provider. Provider failures
surface as typed error codes; the job is never answered with a canned
template pretending to be the model.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from backend.copilot.provider_errors import AIProviderError
from backend.copilot.secret_guard import collect_secret_values, redact_text

STATUS_QUEUED = "queued"
STATUS_THINKING = "thinking"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_CANCELLING = "cancelling"

# In-memory jobs, evicted after this long (mirrors backtest task store —
# single-process deployment, no broker).
JOB_RETENTION_SECONDS = 30 * 60


@dataclass
class ChatJob:
    job_id: str
    question: str
    status: str = STATUS_QUEUED
    resolved_context: Dict[str, Any] = field(default_factory=dict)
    answer: str = ""
    error_code: Optional[str] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)
    completed_at: Optional[float] = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "job_id": self.job_id,
            "status": self.status,
            "question": self.question,
            "created_at": self.created_at,
            "elapsed_seconds": round(
                (self.completed_at or time.monotonic()) - self.created_at, 2
            ),
        }
        if self.status == STATUS_COMPLETED:
            out["answer"] = self.answer
            out["resolved_context"] = self.resolved_context
        elif self.status == STATUS_FAILED:
            out["error_code"] = self.error_code
            out["error"] = self.error
        elif self.status == STATUS_CANCELLED:
            out["error_code"] = "REQUEST_CANCELLED"
            out["error"] = "Generation cancelled."
        return out


# Module-level adapter resolver — defaults to the real settings-driven
# resolution. Tests may replace it (deterministic fake adapters) without
# patching thread-local imports.
def _default_adapter_resolver():
    from backend.copilot.llm_adapter import get_llm_adapter
    return get_llm_adapter


_adapter_resolver = _default_adapter_resolver


class ChatJobManager:
    def __init__(self) -> None:
        self._jobs: Dict[str, ChatJob] = {}
        self._lock = threading.Lock()

    def _evict(self) -> None:
        cutoff = time.monotonic() - JOB_RETENTION_SECONDS
        with self._lock:
            stale = [
                jid for jid, j in self._jobs.items()
                if j.completed_at is not None and j.completed_at < cutoff
            ]
            for jid in stale:
                del self._jobs[jid]

    def submit(
        self,
        question: str,
        resolved_context: Dict[str, Any],
        history: List[Any],
        state: Any = None,
        provider_configured: bool = True,
    ) -> ChatJob:
        """Create a job and start it on a daemon worker thread. Returns
        immediately — provider latency never blocks an HTTP request.
        `state` (ConversationState), when given, receives both turns once
        the answer completes — history is only recorded for REAL
        provider answers, never for failures.

        `provider_configured` is the ROUTER's validated decision (it has
        already answered honestly when no provider is set); the worker
        must not re-derive it from a second settings load, which could
        disagree with the request path."""
        self._evict()
        job = ChatJob(job_id=str(uuid.uuid4()), question=question,
                      resolved_context=resolved_context)
        with self._lock:
            self._jobs[job.job_id] = job

        def _run() -> None:
            from backend.copilot.llm_adapter import (
                RuleBasedFallbackAdapter,
            )
            from backend.copilot.secret_guard import collect_process_secrets

            job.status = STATUS_THINKING
            try:
                if not provider_configured:
                    # Honest configuration answer — NOT a fake AI reply.
                    job.status = STATUS_FAILED
                    job.error_code = "PROVIDER_NOT_CONFIGURED"
                    job.error = (
                        "No AI provider is configured (COPILOT_LLM_BACKEND=none). "
                        "Set COPILOT_LLM_BACKEND=local_openai_compatible (Ollama) "
                        "or openai, then retry. The resolved project context for "
                        "your question is included so the answer is never faked."
                    )
                    return
                if job.cancel_event.is_set():
                    job.status = STATUS_CANCELLED
                    job.completed_at = time.monotonic()
                    return
                # Overridable adapter resolution seam (module-level so tests
                # can inject a deterministic adapter without patching deep
                # imports inside this thread).
                adapter = _adapter_resolver()()
                if isinstance(adapter, RuleBasedFallbackAdapter):
                    job.status = STATUS_FAILED
                    job.error_code = "PROVIDER_NOT_CONFIGURED"
                    job.error = (
                        "No AI provider is configured. The rule-based fallback "
                        "cannot answer open questions honestly — configure a "
                        "provider instead of pretending."
                    )
                    return
                answer = adapter.explain(job.question, job.resolved_context, history=history)
                if job.cancel_event.is_set():
                    job.status = STATUS_CANCELLED
                else:
                    # GROUNDING GUARD: the deterministic context is the
                    # authority. An LLM answer that contradicts it (claims
                    # trades when there are none, denies having data it was
                    # given, claims execution authority) is DISCARDED and the
                    # verified deterministic explanation is served instead.
                    from backend.copilot.grounding_guard import (
                        check_grounding,
                        deterministic_fallback,
                    )
                    violations = check_grounding(answer, job.resolved_context)
                    if violations:
                        logger.warning(
                            "COPILOT_GROUNDING_DISCARD job_id=%s violations=%s",
                            job.job_id, [v["type"] for v in violations],
                        )
                        answer = deterministic_fallback(
                            job.question, job.resolved_context, violations
                        )
                    secrets = collect_secret_values(collect_process_secrets())
                    job.answer = redact_text(answer, secrets)
                    job.status = STATUS_COMPLETED
                    if state is not None:
                        try:
                            state.add_turn("user", job.question)
                            state.add_turn("assistant", job.answer)
                            if job.resolved_context.get("_symbol"):
                                state.last_symbol = job.resolved_context["_symbol"]
                        except Exception:
                            pass
            except AIProviderError as exc:
                job.status = STATUS_FAILED
                job.error_code = exc.code
                job.error = exc.user_message
            except Exception as exc:  # noqa: BLE001 — surfaced, never hidden
                job.status = STATUS_FAILED
                job.error_code = "BACKEND_EXCEPTION"
                job.error = f"Copilot backend error: {type(exc).__name__}: {exc}"
            finally:
                job.completed_at = time.monotonic()

        thread = threading.Thread(target=_run, name=f"copilot-chat-{job.job_id[:8]}", daemon=True)
        job.thread = thread
        thread.start()
        return job

    def get(self, job_id: str) -> Optional[ChatJob]:
        return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> Optional[ChatJob]:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if job.status in (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED):
            return job
        job.cancel_event.set()
        if job.status in (STATUS_QUEUED, STATUS_THINKING):
            job.status = STATUS_CANCELLING
        return job


chat_job_manager = ChatJobManager()
