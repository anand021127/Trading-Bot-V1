"""Start/stop/inspect the Paper worker subprocess from Node bridge or tests."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from backend.database.db_manager import DatabaseManager
from backend.paper.scan_state import compute_runtime_state
from backend.paper.worker_lock import WorkerLock, pid_is_alive
from backend.strategy.trading_engine import BotState

HB_KEY = "paper_worker_heartbeat"
PID_KEY = "paper_worker_pid"
STATUS_KEY = "paper_worker_status"
ERR_KEY = "paper_worker_last_error"
PIPE_KEY = "paper_worker_pipeline_ok"

_ROOT = Path(__file__).resolve().parents[2]
_WORKER_SCRIPT = Path(__file__).resolve().parent / "paper_worker.py"


def _db() -> DatabaseManager:
    path = os.environ.get("DATABASE_PATH", "data/trading_bot.db")
    return DatabaseManager(db_path=path)


def _lock_path(db_path: str) -> str:
    return os.environ.get(
        "PAPER_WORKER_LOCK",
        str(Path(db_path).with_suffix("")) + ".paper_worker.lock",
    )


def _pid_alive(pid: int) -> bool:
    """Cross-platform liveness check (see worker_lock.pid_is_alive)."""
    return pid_is_alive(pid)


def worker_status() -> Dict[str, Any]:
    db = _db()
    BotState._db = db
    pid_raw = db.get_setting(PID_KEY, "")
    try:
        pid = int(pid_raw) if pid_raw else 0
    except ValueError:
        pid = 0
    alive = _pid_alive(pid)
    hb = db.get_setting(HB_KEY, "")
    age = None
    if hb:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(hb)).total_seconds()
        except Exception:
            age = None
    lock = WorkerLock(_lock_path(db.db_path))
    result = {
        "success": True,
        "worker_alive": alive,
        "worker_pid": pid if alive else None,
        "heartbeat_at": hb or None,
        "heartbeat_age_seconds": age,
        "worker_status": db.get_setting(STATUS_KEY, "unknown"),
        "pipeline_ok": db.get_setting(PIPE_KEY, "false") == "true",
        "last_error": db.get_setting(ERR_KEY, "") or None,
        "lock_held": lock.is_foreign_alive() or alive,
        "bot_state": BotState.status(),
        "database_path": db.db_path,
    }
    try:
        # REAL operational state (worker alive + heartbeat + persisted scan
        # record) — never just the BotState flag.
        result["runtime"] = compute_runtime_state(db, pid_alive=_pid_alive)
    except Exception as exc:  # noqa: BLE001
        result["runtime"] = {"state": "UNKNOWN", "label": "Unknown",
                             "summary": f"runtime state unavailable: {type(exc).__name__}"}
    # Close the short-lived handle: this process is transient (one bridge
    # invocation), and leaked open SQLite files block temp-dir cleanup on
    # Windows (PermissionError WinError 32) in e2e tests.
    db.close()
    return result


def start_worker(wait_seconds: float = 8.0) -> Dict[str, Any]:
    """Spawn paper_worker.py if not already running; wait for healthy heartbeat."""
    st = worker_status()
    if st["worker_alive"] and st.get("heartbeat_age_seconds") is not None and st["heartbeat_age_seconds"] < 15:
        # The worker is up but may be IDLE (after Stop it stays alive and only
        # waits for the BotState flag). Start must actually arm it — previously
        # this branch returned success without setting the flag, so pressing
        # Start after Stop reported success while nothing resumed.
        try:
            db0 = _db()
            BotState._db = db0
            BotState.start()
            db0.close()
        except Exception:
            pass
        return {
            "success": True,
            "message": "Paper worker already running",
            "already_running": True,
            **st,
        }

    db = _db()
    BotState._db = db
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("TRADING_MODE", "paper")
    # ABSOLUTE path: the worker is spawned with cwd=<repo root>, while the
    # caller (API / bridge) may run from another cwd. A relative
    # "data/trading_bot.db" would then resolve to two different files and the
    # worker would never see BotState/scan state written by the API.
    env["DATABASE_PATH"] = str(Path(db.db_path).resolve())

    # Preflight env (same rules as runtime) so UI gets a clear error before spawn
    strategy = env.get("TRADING_STRATEGY", "").strip()
    product = env.get("UPSTOX_ORDER_PRODUCT", "").strip().upper()
    if strategy != "V8_D_PULLBACK_ATM":
        return {
            "success": False,
            "message": "Paper mode requires TRADING_STRATEGY=V8_D_PULLBACK_ATM",
        }
    if product not in {"I", "D"}:
        return {
            "success": False,
            "message": "Paper mode requires UPSTOX_ORDER_PRODUCT=I or D",
        }
    if env.get("TRADING_MODE", "paper").lower() != "paper":
        return {"success": False, "message": "TRADING_MODE must be paper"}

    log_path = Path(db.db_path).parent / "paper_worker.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(log_path, "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(_WORKER_SCRIPT)],
            cwd=str(_ROOT),
            env=env,
            stdout=log_f,
            stderr=log_f,
            start_new_session=True,
        )
    except Exception as exc:
        return {"success": False, "message": f"Failed to spawn worker: {type(exc).__name__}: {exc}"}

    deadline = time.time() + wait_seconds
    last: Dict[str, Any] = {}
    while time.time() < deadline:
        time.sleep(0.4)
        last = worker_status()
        if last.get("worker_alive") and last.get("pipeline_ok"):
            BotState.start()
            return {
                "success": True,
                "message": "Paper worker started and pipeline armed",
                "spawned_pid": proc.pid,
                "log_path": str(log_path),
                **last,
            }
        # Detect immediate crash
        if proc.poll() is not None:
            err = db.get_setting(ERR_KEY, "") or f"worker exited code={proc.returncode}"
            return {
                "success": False,
                "message": f"Paper worker failed to stay up: {err}",
                "log_path": str(log_path),
                **worker_status(),
            }

    # Timeout
    err = last.get("last_error") or "heartbeat timeout"
    return {
        "success": False,
        "message": f"Paper worker did not become healthy within {wait_seconds}s: {err}",
        "log_path": str(log_path),
        **worker_status(),
    }


def stop_worker() -> Dict[str, Any]:
    db = _db()
    BotState._db = db
    BotState.stop("Manual stop via dashboard")
    try:
        st = worker_status()
        pid = st.get("worker_pid")
        if pid and _pid_alive(int(pid)):
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError as exc:
                return {"success": False, "message": f"kill failed: {exc}", **worker_status()}
            # wait for exit
            for _ in range(25):
                time.sleep(0.2)
                if not _pid_alive(int(pid)):
                    break
            else:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except OSError:
                    pass
        db.save_setting(STATUS_KEY, "stopped")
    finally:
        db.close()
    return {"success": True, "message": "Paper worker stopped", **worker_status()}


def kill_worker() -> Dict[str, Any]:
    from backend.execution.kill_switch import FULL_SYSTEM_STOP, PersistentKillSwitch

    db = _db()
    BotState._db = db
    BotState.kill("Emergency kill switch activated from dashboard")
    try:
        PersistentKillSwitch(db).set_level(FULL_SYSTEM_STOP, "dashboard_kill")
    except Exception:
        pass
    st = worker_status()
    pid = st.get("worker_pid")
    if pid and _pid_alive(int(pid)):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass
        time.sleep(0.5)
        if _pid_alive(int(pid)):
            try:
                os.kill(int(pid), signal.SIGKILL)
            except OSError:
                pass
    db.save_setting(STATUS_KEY, "killed")
    db.close()
    return {
        "success": True,
        "message": "EMERGENCY KILL ACTIVATED. Paper worker stopped.",
        **worker_status(),
    }


def full_health() -> Dict[str, Any]:
    db = _db()
    BotState._db = db
    w = worker_status()
    token = db.load_token(require_valid=False) or os.environ.get("UPSTOX_ACCESS_TOKEN", "")
    market = {
        "token_present": bool(token),
        "note": "Market data requires valid Upstox token; paper fills do not hit the live exchange",
    }
    return {
        "success": True,
        "node": {"ok": True, "role": "ui_gateway"},
        "python_worker": {
            "ok": bool(w.get("worker_alive")),
            "pid": w.get("worker_pid"),
            "status": w.get("worker_status"),
            "heartbeat_age_seconds": w.get("heartbeat_age_seconds"),
            "pipeline_ok": w.get("pipeline_ok"),
            "last_error": w.get("last_error"),
        },
        "database": {"ok": True, "path": db.db_path},
        "market_data": market,
        "trading_engine": {
            "ok": bool(w.get("pipeline_ok")),
            "executor": "PaperTradingRuntime",
            "bot_running": bool(w.get("bot_state", {}).get("running")),
            "kill_switch_active": bool(w.get("bot_state", {}).get("kill_switch_active")),
        },
        "paper_runtime": {
            "ok": bool(w.get("worker_alive") and w.get("pipeline_ok")),
            "strategy": os.environ.get("TRADING_STRATEGY", ""),
            "product": os.environ.get("UPSTOX_ORDER_PRODUCT", ""),
        },
    }


# ── API-side helpers (FastAPI /api/bot/start + watchdog) ─────────────────────
def autospawn_enabled() -> bool:
    """Whether the API may spawn the paper worker process itself.

    Default ON in real deployments; OFF under the offline unit-test flag
    (tests must never fork workers unless they opt in with
    PAPER_WORKER_AUTOSPAWN=1). Set PAPER_WORKER_AUTOSPAWN=0 when the worker is
    supervised externally (e.g. its own systemd unit)."""
    v = os.environ.get("PAPER_WORKER_AUTOSPAWN", "").strip().lower()
    if v in {"0", "false", "no", "off"}:
        return False
    if v in {"1", "true", "yes", "on"}:
        return True
    return os.environ.get("TRADING_BOT_OFFLINE_TESTS", "").strip() != "1"


def ensure_worker_running(wait_seconds: float = 10.0) -> Dict[str, Any]:
    """Make sure a paper worker process exists (spawn if needed) and BotState
    is set. Paper-only: refuses any non-paper mode. Idempotent."""
    if os.environ.get("TRADING_MODE", "paper").strip().lower() != "paper":
        return {"success": False, "message": "TRADING_MODE must be paper"}
    return start_worker(wait_seconds=wait_seconds)


_WATCHDOG: Dict[str, Any] = {"attempts": 0, "last_attempt": 0.0}
WATCHDOG_MIN_GAP_SECONDS = 60.0
WATCHDOG_MAX_ATTEMPTS = 5


def watchdog_check(now_mono: Optional[float] = None) -> Dict[str, Any]:
    """If the bot is flagged RUNNING but the paper worker is dead/hung, respawn
    it (bounded: 1 attempt / 60s, max 5 consecutive). Returns what it did.
    Never runs when the kill switch is active or the bot is stopped."""
    now_mono = time.monotonic() if now_mono is None else now_mono
    st = worker_status()
    bot = st.get("bot_state") or {}
    if not bot.get("running") or bot.get("kill_switch_active"):
        _WATCHDOG["attempts"] = 0
        return {"action": "none", "reason": "bot not running"}
    age = st.get("heartbeat_age_seconds")
    healthy = bool(st.get("worker_alive")) and age is not None and age < 30
    if healthy:
        _WATCHDOG["attempts"] = 0
        return {"action": "none", "reason": "worker healthy"}
    if _WATCHDOG["attempts"] >= WATCHDOG_MAX_ATTEMPTS:
        return {"action": "gave_up", "reason": f"{WATCHDOG_MAX_ATTEMPTS} respawn attempts failed",
                "last_error": st.get("last_error")}
    if now_mono - float(_WATCHDOG["last_attempt"]) < WATCHDOG_MIN_GAP_SECONDS:
        return {"action": "backoff", "reason": "waiting before next respawn attempt"}
    _WATCHDOG["last_attempt"] = now_mono
    _WATCHDOG["attempts"] += 1
    res = ensure_worker_running()
    return {"action": "respawn", "attempt": _WATCHDOG["attempts"],
            "success": bool(res.get("success")), "message": res.get("message")}
