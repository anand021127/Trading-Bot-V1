"""The ONE authoritative enum for backtest job lifecycle states.

Every status value crossing the API (and rendered by the frontend) must come
from this module — backend `task_manager`, routers, and the frontend's
`BacktestStatus` type all reference these exact string values. Adding a new
lifecycle state means adding it here, to the task manager transitions, and to
the frontend union — nothing else.
"""
from __future__ import annotations

from enum import Enum


class BacktestStatus(str, Enum):
    QUEUED = "QUEUED"
    FETCHING_DATA = "FETCHING_DATA"
    RESOLVING_CONTRACTS = "RESOLVING_CONTRACTS"
    RUNNING = "RUNNING"
    FINALIZING = "FINALIZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED_BY_RESTART = "INTERRUPTED_BY_RESTART"


# Plain string constants (API-facing; the enum values are the wire format).
QUEUED = BacktestStatus.QUEUED.value
FETCHING_DATA = BacktestStatus.FETCHING_DATA.value
RESOLVING_CONTRACTS = BacktestStatus.RESOLVING_CONTRACTS.value
RUNNING = BacktestStatus.RUNNING.value
FINALIZING = BacktestStatus.FINALIZING.value
COMPLETED = BacktestStatus.COMPLETED.value
FAILED = BacktestStatus.FAILED.value
CANCELLED = BacktestStatus.CANCELLED.value
INTERRUPTED_BY_RESTART = BacktestStatus.INTERRUPTED_BY_RESTART.value

# All valid wire values, exactly one place.
ALL_STATUSES: frozenset = frozenset(s.value for s in BacktestStatus)

# Terminal states: no further transitions allowed.
TERMINAL_STATUSES: frozenset = frozenset({
    COMPLETED, FAILED, CANCELLED, INTERRUPTED_BY_RESTART,
})

# States in which a job can still make progress.
ACTIVE_STATUSES: frozenset = ALL_STATUSES - TERMINAL_STATUSES


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def is_active(status: str) -> bool:
    return status in ACTIVE_STATUSES


def normalize_status(value: str) -> str:
    """Map legacy/lowercase spellings onto the authoritative enum.

    Existing DB rows or clients that wrote lowercase equivalents are mapped
    onto the canonical uppercase values so old data keeps rendering correctly.
    """
    if not value:
        return ""
    v = str(value).strip().upper()
    if v in ALL_STATUSES:
        return v
    legacy = {
        "QUEUED": QUEUED, "FETCHING_DATA": FETCHING_DATA, "RUNNING": RUNNING,
        "COMPLETED": COMPLETED, "FAILED": FAILED, "CANCELLED": CANCELLED,
        "FINALIZING": FINALIZING, "RESOLVING_CONTRACTS": RESOLVING_CONTRACTS,
    }
    return legacy.get(v, v)


__all__ = [
    "BacktestStatus",
    "QUEUED", "FETCHING_DATA", "RESOLVING_CONTRACTS", "RUNNING", "FINALIZING",
    "COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED_BY_RESTART",
    "ALL_STATUSES", "TERMINAL_STATUSES", "ACTIVE_STATUSES",
    "is_terminal", "is_active", "normalize_status",
]
