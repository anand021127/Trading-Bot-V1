"""Single-instance lock for the Paper worker process."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional


class WorkerLockError(RuntimeError):
    pass


class WorkerLock:
    def __init__(self, lock_path: str) -> None:
        self.lock_path = Path(lock_path)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._held = False

    def _pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

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
