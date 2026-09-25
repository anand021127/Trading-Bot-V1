"""Single-instance lock for the Paper worker process."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional


class WorkerLockError(RuntimeError):
    pass


def pid_is_alive(pid: int) -> bool:
    """Cross-platform PID liveness check.

    ``os.kill(pid, 0)`` is the POSIX signal-probe idiom. On Windows,
    os.kill with signal 0 opens the process with PROCESS_TERMINATE rights:
    it can raise PermissionError for processes you cannot terminate even
    though they are alive, and it does not behave like the POSIX probe.
    Production bug fixed: on Windows hosts the API layer reported the
    freshly-spawned paper worker as dead (worker_alive=false with a fresh
    heartbeat), which flipped the dashboard to "not running" while the
    worker was actually trading.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return exit_code.value == STILL_ACTIVE
                return False
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user — it is alive.
        return True
    except OSError:
        return False


class WorkerLock:
    def __init__(self, lock_path: str) -> None:
        self.lock_path = Path(lock_path)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._held = False

    def _pid_alive(self, pid: int) -> bool:
        return pid_is_alive(pid)

    def read_pid(self) -> Optional[int]:
        try:
            raw = self.lock_path.read_text().strip()
            return int(raw) if raw else None
        except Exception:
            return None

    def acquire(self) -> None:
        existing = self.read_pid()
        if existing and self._pid_alive(existing):
            if existing == os.getpid():
                self._held = True
                return
            raise WorkerLockError(f"Paper worker already running (pid={existing})")
        # Stale lock
        self.lock_path.write_text(str(os.getpid()))
        self._held = True

    def release(self) -> None:
        if not self._held:
            return
        try:
            pid = self.read_pid()
            if pid == os.getpid() and self.lock_path.exists():
                self.lock_path.unlink()
        except Exception:
            pass
        self._held = False

    def is_foreign_alive(self) -> bool:
        pid = self.read_pid()
        return bool(pid and pid != os.getpid() and self._pid_alive(pid))
