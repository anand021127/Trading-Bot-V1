"""Task supervisor — watches critical background asyncio tasks and auto-
restarts them when they die unexpectedly.

Why this exists
---------------
The scanner, heartbeat, and (future) position-monitor tasks are created via
``asyncio.create_task()`` during startup.  If any of them raise an unhandled
exception, the task silently terminates — nothing in the current codebase
detects or recovers that.  This supervisor runs its own lightweight periodic
check (every ``check_interval_seconds``) and restarts dead tasks up to
``max_restarts`` times, after which it marks the component FAILED so the
dashboard can surface the issue instead of silently pretending everything is
fine.

Duplicate-task prevention
-------------------------
Before restarting, the supervisor verifies the existing task is actually done
(``task.done()``).  It never creates a second concurrent instance.
"""
from __future__ import annotations

import asyncio
import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from backend.health.health_monitor import (
    ComponentStatus,
    health_monitor,
)

logger = logging.getLogger(__name__)

# How often the supervisor checks task liveness
DEFAULT_CHECK_INTERVAL = 10  # seconds

# How many times a task can be auto-restarted before being marked FAILED
DEFAULT_MAX_RESTARTS = 5

# Backoff between restarts (seconds) — linear: attempt * backoff_factor
DEFAULT_BACKOFF_FACTOR = 5


@dataclass
class ManagedTask:
    """A background task under supervisor management."""
    name: str
    factory: Callable[[], Awaitable[None]]
    task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
    started_at: float = 0.0
    restart_count: int = 0
    last_exception: Optional[str] = None
    last_exception_time: float = 0.0
    max_restarts: int = DEFAULT_MAX_RESTARTS

    def to_dict(self) -> Dict[str, Any]:
        now = time.monotonic()
        alive = self.task is not None and not self.task.done()
        return {
            "name": self.name,
            "alive": alive,
            "started_seconds_ago": round(now - self.started_at, 1) if self.started_at else None,
            "restart_count": self.restart_count,
            "last_exception": self.last_exception,
            "last_exception_seconds_ago": (
                round(now - self.last_exception_time, 1)
                if self.last_exception_time else None
            ),
        }


class TaskSupervisor:
    """Watches registered asyncio tasks and restarts them on failure."""

    def __init__(
        self,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
    ) -> None:
        self._tasks: Dict[str, ManagedTask] = {}
        self._check_interval = check_interval
        self._supervisor_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
        self._running = False

    # ── registration ────────────────────────────────────────────────────

    def register(
        self,
        name: str,
        factory: Callable[[], Awaitable[None]],
        max_restarts: int = DEFAULT_MAX_RESTARTS,
    ) -> None:
        """Register a task factory.  ``factory`` is a zero-arg async callable
        that returns an awaitable (typically an async function reference).
        The supervisor calls it to create/restart the task."""
        self._tasks[name] = ManagedTask(
            name=name,
            factory=factory,
            max_restarts=max_restarts,
        )
        health_monitor.register(name)

    # ── lifecycle ───────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the supervisor itself as a background task."""
        if self._supervisor_task is not None and not self._supervisor_task.done():
            return
        self._running = True
        self._supervisor_task = asyncio.ensure_future(self._run())
        logger.info("TaskSupervisor started — monitoring %d tasks", len(self._tasks))

    def stop(self) -> None:
        self._running = False
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
        # Cancel all managed tasks
        for mt in self._tasks.values():
            if mt.task is not None and not mt.task.done():
                mt.task.cancel()

    # ── start a specific managed task ───────────────────────────────────

    def _start_task(self, mt: ManagedTask) -> None:
        """Create an asyncio.Task from the factory and record it."""
        if mt.task is not None and not mt.task.done():
            logger.debug("Task %s is still alive — skipping start", mt.name)
            return
        try:
            coro = mt.factory()
            mt.task = asyncio.ensure_future(coro)
            mt.started_at = time.monotonic()
            health_monitor.update_status(mt.name, ComponentStatus.RUNNING)
            health_monitor.log_event(
                mt.name, "TASK_STARTED",
                f"Task {mt.name} started (restart #{mt.restart_count})",
            )
            logger.info("Started managed task: %s", mt.name)
        except Exception as e:
            mt.last_exception = str(e)
            mt.last_exception_time = time.monotonic()
            health_monitor.update_status(mt.name, ComponentStatus.FAILED)
            health_monitor.record_error(mt.name, str(e))
            health_monitor.log_event(
                mt.name, "TASK_START_FAILED",
                f"Failed to create task {mt.name}: {e}",
                severity="ERROR",
                exception=traceback.format_exc(),
            )
            logger.error("Failed to start managed task %s: %s", mt.name, e)

    # ── supervisor loop ─────────────────────────────────────────────────

    async def _run(self) -> None:
        """Periodically check all managed tasks and restart dead ones."""
        # Initial start of all registered tasks
        for mt in self._tasks.values():
            self._start_task(mt)

        while self._running:
            try:
                await asyncio.sleep(self._check_interval)
                self._check_all()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("TaskSupervisor loop error: %s", e)
                await asyncio.sleep(self._check_interval)

    def _check_all(self) -> None:
        for mt in self._tasks.values():
            if mt.task is None:
                # Never started — start it
                self._start_task(mt)
                continue

            if not mt.task.done():
                # Task is alive — just heartbeat
                health_monitor.heartbeat(mt.name)
                continue

            # Task is dead — inspect why
            exception = mt.task.exception() if not mt.task.cancelled() else None
            exc_str = str(exception) if exception else "cancelled/unknown"
            tb_str = ""
            if exception:
                try:
                    tb_str = "".join(traceback.format_exception(
                        type(exception), exception, exception.__traceback__,
                    ))
                except Exception:
                    tb_str = exc_str

            mt.last_exception = exc_str
            mt.last_exception_time = time.monotonic()
            health_monitor.record_error(mt.name, exc_str)
            health_monitor.log_event(
                mt.name, "TASK_DIED",
                f"Task {mt.name} died: {exc_str}",
                severity="ERROR",
                exception=tb_str or None,
            )
            logger.error("Managed task %s died: %s", mt.name, exc_str)

            # Check restart budget
            if mt.restart_count >= mt.max_restarts:
                health_monitor.update_status(mt.name, ComponentStatus.FAILED)
                health_monitor.log_event(
                    mt.name, "TASK_RESTART_LIMIT",
                    f"Task {mt.name} exceeded max restarts ({mt.max_restarts}). "
                    f"Marking FAILED — manual intervention required.",
                    severity="CRITICAL",
                )
                logger.critical(
                    "Task %s has exceeded max restarts (%d). NOT restarting.",
                    mt.name, mt.max_restarts,
                )
                continue

            # Restart with backoff
            mt.restart_count += 1
            health_monitor.record_restart(mt.name)
            health_monitor.update_status(mt.name, ComponentStatus.DEGRADED)
            backoff = mt.restart_count * DEFAULT_BACKOFF_FACTOR
            logger.warning(
                "Restarting task %s (attempt %d/%d) after %ds backoff",
                mt.name, mt.restart_count, mt.max_restarts, backoff,
            )
            # Use a brief delay then restart (non-blocking relative to other tasks)
            asyncio.ensure_future(self._delayed_restart(mt, backoff))

    async def _delayed_restart(self, mt: ManagedTask, delay: float) -> None:
        await asyncio.sleep(delay)
        # A restart can be queued just before application shutdown.  Do not
        # resurrect a managed task after ``stop()`` has cancelled it.
        if not self._running:
            return
        # Double-check it's still dead (another restart may have beaten us)
        if mt.task is not None and not mt.task.done():
            return
        self._start_task(mt)

    # ── status snapshot ─────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        return {
            "supervisor_running": self._running,
            "tasks": {
                name: mt.to_dict() for name, mt in self._tasks.items()
            },
        }
