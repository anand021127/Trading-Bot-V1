"""Typed provider errors for the Copilot AI backend.

Root cause addressed: every LLM failure used to be silently converted
into a rule-based canned answer (or, at the HTTP layer, into a generic
error the frontend could not distinguish from a network timeout). The
operator could not tell "provider unreachable" from "wrong API key"
from "model not downloaded".

Each failure mode now carries a stable machine-readable `code` plus a
human-readable `user_message`. The frontend maps `code` to distinct UI
states; it NEVER collapses every error into
"timeout of 30000ms exceeded".
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class AIProviderError(Exception):
    """Base class for all Copilot AI-provider failures."""

    code = "PROVIDER_ERROR"
    http_status = 502
    user_message = "The AI provider failed. Check backend logs for details."

    def __init__(self, detail: Optional[str] = None) -> None:
        self.detail = detail or self.user_message
        super().__init__(self.detail)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error_code": self.code,
            "message": self.user_message,
            "detail": self.detail,
        }


class AIProviderUnavailableError(AIProviderError):
    """Provider endpoint could not be reached at all (connection refused,
    DNS failure, server down). Distinct from a timeout."""

    code = "PROVIDER_UNAVAILABLE"
    http_status = 503
    user_message = (
        "AI provider is unreachable. If you use a local provider (Ollama), "
        "make sure it is running; otherwise check COPILOT_LLM_BASE_URL."
    )


class AIProviderTimeoutError(AIProviderError):
    """The provider accepted the connection but did not answer in time."""

    code = "PROVIDER_TIMEOUT"
    http_status = 504
    user_message = (
        "The AI provider did not respond in time. The request can be "
        "retried; this is NOT a frontend timeout."
    )


class AIProviderAuthError(AIProviderError):
    """Provider rejected the credentials (HTTP 401/403)."""

    code = "PROVIDER_AUTH_FAILED"
    http_status = 502
    user_message = (
        "The AI provider rejected the configured credentials. Check the "
        "provider's API key configuration — it is never sent to or shown "
        "by the Copilot."
    )


class AIProviderRateLimitError(AIProviderError):
    """Provider rate limit hit (HTTP 429). Safe to retry after a delay."""

    code = "PROVIDER_RATE_LIMITED"
    http_status = 429
    user_message = "The AI provider is rate limiting requests. Retry shortly."


class AIModelUnavailableError(AIProviderError):
    """The configured model does not exist / is not loaded (HTTP 404 with
    model_not_found, or provider-specific equivalents)."""

    code = "MODEL_UNAVAILABLE"
    http_status = 502
    user_message = (
        "The configured AI model is not available on the provider. Check "
        "COPILOT_LLM_MODEL (e.g. is the model downloaded/loaded?)."
    )


class AIRequestCancelledError(AIProviderError):
    """The user cancelled generation — not a failure of the provider."""

    code = "REQUEST_CANCELLED"
    http_status = 499
    user_message = "Generation cancelled."


def classify_provider_exception(exc: Exception) -> AIProviderError:
    """Map a raw provider-side exception to a typed AIProviderError.

    Never raises anything itself except AIProviderError subclasses —
    genuinely unknown exceptions become AIProviderError (code
    PROVIDER_ERROR) with the original message preserved in `detail`.
    """
    if isinstance(exc, AIProviderError):
        return exc
    import socket
    import urllib.error
    import urllib.request  # noqa: F401 — kept for isinstance parity/docs

    # urllib raises URLError for connection problems; socket.timeout for
    # read timeouts (URLError(reason=timeout) also possible).
    if isinstance(exc, TimeoutError) or isinstance(exc, socket.timeout):
        return AIProviderTimeoutError(str(exc))
    if isinstance(exc, urllib.error.HTTPError):
        status = exc.code
        reason = str(exc)
        if status in (401, 403):
            return AIProviderAuthError(reason)
        if status == 404:
            return AIModelUnavailableError(reason)
        if status == 429:
            return AIProviderRateLimitError(reason)
        if status >= 500:
            return AIProviderUnavailableError(reason)
        return AIProviderError(reason)
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return AIProviderTimeoutError(str(reason))
        return AIProviderUnavailableError(str(reason))
    if isinstance(exc, (ConnectionError, OSError)):
        return AIProviderUnavailableError(str(exc))
    return AIProviderError(f"{type(exc).__name__}: {exc}")
