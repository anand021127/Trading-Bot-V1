"""Retry-with-backoff for historical Upstox API fetches used by backtests.

Root cause addressed: a single transient historical-API timeout (Upstox
HTTP 408/429/503, or a client socket timeout) used to abort the whole
backtest job with DATA_UNAVAILABLE — even though the very same request
succeeds seconds later. The frontend would then show that raw backend
failure as if the *architecture* itself had timed out.

Fix: bounded retry with exponential backoff at the full-range-fetch
boundary — the exact seam where one slow request used to kill the job.

Deliberately NOT:
- unlimited retries (bounded, so a genuinely down API fails the job
  fast and honestly with the real reason)
- silent data substitution (only retries the SAME real request)
- a change to chunking granularity (a separate concern; this wrapper
  only re-issues identical requests)

Timeouts vs other errors: socket timeouts (408 / socket.timeout) are
retried because they are transient; 429 (rate limit) is retried with a
longer backoff; 4xx client errors like 401/403 are NOT retried because
retrying an auth failure cannot succeed.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Default retry policy for historical fetches. Kept modest so a full
# multi-symbol backtest cannot hang for an unbounded time: worst case
# per symbol is 3 attempts of the underlying request plus ~15s of sleep.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY_SECONDS = 2.0
DEFAULT_MAX_DELAY_SECONDS = 10.0


def _status_code(exc: BaseException) -> Optional[int]:
    return getattr(exc, "status_code", None)


def _is_retriable(exc: BaseException) -> bool:
    """Timeouts/rate-limit/availability errors are retriable; auth and
    bad-request errors are not (retrying them cannot succeed)."""
    code = _status_code(exc)
    if code is not None:
        if code == 429:
            return True
        return code >= 500 or code == 408
    # No status code: only retry things that look like timeouts —
    # never retry ValueError/TypeError-style programming errors.
    name = type(exc).__name__.lower()
    return "timeout" in name or "timedout" in name


def fetch_full_range_with_retry(
    fetch: Callable[[], List[Dict[str, Any]]],
    *,
    symbol: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY_SECONDS,
    max_delay: float = DEFAULT_MAX_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> List[Dict[str, Any]]:
    """Run `fetch()` with bounded retry/backoff for transient failures.

    Re-raises the LAST exception unchanged when all attempts fail, so the
    caller's existing error reporting (which already surfaces the real
    backend reason to the job status) shows the actual failure, not a
    generic "timed out".
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fetch()
        except Exception as exc:  # noqa: BLE001 — classified below
            last_exc = exc
            if attempt >= max_attempts or not _is_retriable(exc):
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            if _status_code(exc) == 429:
                delay = max(delay, base_delay * 2)
            logger.warning(
                "BACKTEST_HISTORICAL_FETCH_RETRY symbol=%s attempt=%d/%d "
                "retrying_in=%.1fs error=%s",
                symbol, attempt, max_attempts, delay, exc,
            )
            sleep(delay)
    # Unreachable (loop either returns or raises) — kept for type-checkers.
    raise last_exc  # type: ignore[misc]
