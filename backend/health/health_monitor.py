"""Centralised health monitor — single source of truth for every component's
real-time status.

Every background subsystem (scanner, WebSocket, trading engine, position
monitor, etc.) updates its own entry here on every heartbeat / cycle
completion.  The ``/api/bot/status`` and ``/api/health`` endpoints read this
object directly — they never need to perform expensive broker calls or wait
for a scanner cycle just to answer "is it alive?".

States per component
--------------------
STARTING      — component initialising, not yet operational
RUNNING       — healthy, operating normally
DEGRADED      — operating but with errors (e.g. retrying after failure)
PAUSED        — intentionally paused (e.g. outside market hours)
RECONNECTING  — temporarily disconnected, attempting recovery
STOPPED       — intentionally stopped (user action / shutdown)
FAILED        — stopped after exceeding retry limits
UNKNOWN       — no heartbeat received yet
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ComponentStatus(str, Enum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    PAUSED = "PAUSED"
    RECONNECTING = "RECONNECTING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class MarketDataStatus(str, Enum):
    LIVE = "LIVE"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass
class ComponentHealth:
    """Health state for a single component."""
    name: str
    status: ComponentStatus = ComponentStatus.UNKNOWN
    last_heartbeat: float = 0.0          # time.monotonic()
    last_success: float = 0.0
    last_error_time: float = 0.0
    last_error: Optional[str] = None
    error_count: int = 0
    restart_count: int = 0
    last_restart_time: float = 0.0
    started_at: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)

    def heartbeat(self, *, extra: Optional[Dict[str, Any]] = None) -> None:
        now = time.monotonic()
        self.last_heartbeat = now
        self.last_success = now
        if extra:
            self.extra.update(extra)

    def record_error(self, error: str) -> None:
        self.last_error = error
        self.last_error_time = time.monotonic()
        self.error_count += 1

    def record_restart(self) -> None:
        self.restart_count += 1
        self.last_restart_time = time.monotonic()

    def to_dict(self) -> Dict[str, Any]:
        now = time.monotonic()
        return {
            "name": self.name,
            "status": self.status.value,
            "last_heartbeat_seconds_ago": (
                round(now - self.last_heartbeat, 1) if self.last_heartbeat else None
            ),
            "last_success_seconds_ago": (
                round(now - self.last_success, 1) if self.last_success else None
            ),
            "last_error": self.last_error,
            "last_error_seconds_ago": (
                round(now - self.last_error_time, 1) if self.last_error_time else None
            ),
            "error_count": self.error_count,
            "restart_count": self.restart_count,
            "extra": self.extra,
        }


@dataclass
class LifecycleEvent:
    timestamp: str
    component: str
    event: str
    severity: str  # INFO, WARNING, ERROR, CRITICAL
    message: str
    exception: Optional[str] = None


# Maximum lifecycle events kept in memory
MAX_LIFECYCLE_EVENTS = 200


class HealthMonitor:
    """Singleton-style health monitor.  Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._components: Dict[str, ComponentHealth] = {}
        self._lifecycle_events: List[LifecycleEvent] = []
        self._startup_timestamp = datetime.now(timezone.utc).isoformat()
        self._startup_mono = time.monotonic()
        self._process_id = os.getpid()
        self._last_heartbeat_mono: float = 0.0  # global heartbeat
        self._restart_count = 0  # process-level restarts (detected externally)

    # ── component registration ──────────────────────────────────────────

    def register(self, name: str) -> ComponentHealth:
        with self._lock:
            if name not in self._components:
                self._components[name] = ComponentHealth(name=name)
            return self._components[name]

    def get(self, name: str) -> Optional[ComponentHealth]:
        with self._lock:
            return self._components.get(name)

    # ── state updates (called by subsystems) ────────────────────────────

    def update_status(self, name: str, status: ComponentStatus, **extra: Any) -> None:
        with self._lock:
            comp = self._components.get(name)
            if comp is None:
                comp = ComponentHealth(name=name)
                self._components[name] = comp
            comp.status = status
            if extra:
                comp.extra.update(extra)

    def heartbeat(self, name: str, **extra: Any) -> None:
        with self._lock:
            comp = self._components.get(name)
            if comp is None:
                comp = ComponentHealth(name=name)
                self._components[name] = comp
            comp.heartbeat(extra=extra if extra else None)
            self._last_heartbeat_mono = time.monotonic()

    def record_error(self, name: str, error: str) -> None:
        with self._lock:
            comp = self._components.get(name)
            if comp is None:
                comp = ComponentHealth(name=name)
                self._components[name] = comp
            comp.record_error(error)

    def record_restart(self, name: str) -> None:
        with self._lock:
            comp = self._components.get(name)
            if comp is None:
                comp = ComponentHealth(name=name)
                self._components[name] = comp
            comp.record_restart()

    # ── lifecycle events ────────────────────────────────────────────────

    def log_event(
        self,
        component: str,
        event: str,
        message: str,
        severity: str = "INFO",
        exception: Optional[str] = None,
    ) -> None:
        entry = LifecycleEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            component=component,
            event=event,
            severity=severity,
            message=message,
            exception=exception,
        )
        with self._lock:
            self._lifecycle_events.append(entry)
            if len(self._lifecycle_events) > MAX_LIFECYCLE_EVENTS:
                self._lifecycle_events = self._lifecycle_events[-MAX_LIFECYCLE_EVENTS:]

        # Also emit to standard Python logging so it appears in stdout/logs
        log_level = getattr(logging, severity.upper(), logging.INFO)
        logger.log(log_level, "[%s] %s — %s%s", component, event, message,
                   f" | exception={exception}" if exception else "")

    # ── global heartbeat (called by the heartbeat background task) ──────

    def global_heartbeat(self) -> None:
        self._last_heartbeat_mono = time.monotonic()

    # ── aggregate snapshot (for the API) ────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            components = {
                name: comp.to_dict()
                for name, comp in self._components.items()
            }
            # Determine overall bot status from component states
            statuses = [c.status for c in self._components.values()]
            if ComponentStatus.FAILED in statuses:
                overall = "DEGRADED"
            elif all(s == ComponentStatus.RUNNING for s in statuses if s not in (
                ComponentStatus.STOPPED, ComponentStatus.PAUSED,
            )):
                overall = "HEALTHY"
            elif any(s in (ComponentStatus.DEGRADED, ComponentStatus.RECONNECTING) for s in statuses):
                overall = "DEGRADED"
            elif all(s == ComponentStatus.STOPPED for s in statuses):
                overall = "STOPPED"
            else:
                overall = "UNKNOWN" if not statuses else "DEGRADED"

            return {
                "bot_status": overall,
                "uptime_seconds": round(now - self._startup_mono, 1),
                "started_at": self._startup_timestamp,
                "process_id": self._process_id,
                "last_heartbeat_seconds_ago": (
                    round(now - self._last_heartbeat_mono, 1)
                    if self._last_heartbeat_mono else None
                ),
                "components": components,
                "recent_events": [
                    {
                        "timestamp": e.timestamp,
                        "component": e.component,
                        "event": e.event,
                        "severity": e.severity,
                        "message": e.message,
                        "exception": e.exception,
                    }
                    for e in self._lifecycle_events[-20:]  # last 20 events
                ],
            }

    def get_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            events = self._lifecycle_events[-limit:]
        return [
            {
                "timestamp": e.timestamp,
                "component": e.component,
                "event": e.event,
                "severity": e.severity,
                "message": e.message,
                "exception": e.exception,
            }
            for e in reversed(events)
        ]


# ── module-level singleton ──────────────────────────────────────────────
health_monitor = HealthMonitor()
