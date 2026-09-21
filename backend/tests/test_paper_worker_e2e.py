"""End-to-end Paper worker lifecycle tests (no live orders)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "backend" / "cli" / "node_bridge.py"


def _env(db_path: str, **extra) -> dict:
    e = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "DATABASE_PATH": db_path,
        "PAPER_WORKER_LOCK": db_path + ".lock",
        "TRADING_MODE": "paper",
        "TRADING_STRATEGY": "V8_D_PULLBACK_ATM",
        "UPSTOX_ORDER_PRODUCT": "I",
        "TRADING_CAPITAL": "100000",
        "RISK_PER_TRADE_PCT": "0.025",
        "MAX_ALLOCATION_PCT": "0.18",
        "PAPER_WORKER_INTERVAL_SEC": "1",
        "PAPER_WORKER_START_WAIT": "12",
        "PAPER_ALLOW_TEST_SIGNAL": "1",
    }
    e.update(extra)
    return e


def _bridge(cmd: str, env: dict) -> dict:
    out = subprocess.check_output(
        [sys.executable, str(SCRIPT), cmd],
        env=env,
        cwd=str(ROOT),
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    line = out.decode().strip().splitlines()[-1]
    return json.loads(line)


def test_start_spawns_worker_and_pipeline():
    path = os.path.join(tempfile.mkdtemp(), "pw.db")
    env = _env(path)
    try:
        start = _bridge("start", env)
        assert start.get("success") is True, start
        st = _bridge("status", env)
        assert st.get("worker_alive") is True, st
        assert st.get("pipeline_ok") is True, st
        assert st.get("running") is True, st
        health = _bridge("health", env)
        assert health["python_worker"]["ok"] is True
        assert health["paper_runtime"]["ok"] is True
        assert health["database"]["ok"] is True
    finally:
        _bridge("stop", env)


def test_stop_kills_worker_process():
    path = os.path.join(tempfile.mkdtemp(), "pw2.db")
    env = _env(path)
    start = _bridge("start", env)
    assert start.get("success") is True, start
    pid = start.get("worker_pid")
    assert pid
    stop = _bridge("stop", env)
    assert stop.get("success") is True, stop
    time.sleep(0.8)
    st = _bridge("status", env)
    assert st.get("worker_alive") is False, st
    assert st.get("running") is False, st


def test_duplicate_start_does_not_spawn_second_worker():
    path = os.path.join(tempfile.mkdtemp(), "pw3.db")
    env = _env(path)
    try:
        a = _bridge("start", env)
        assert a.get("success") is True, a
        pid1 = a.get("worker_pid")
        b = _bridge("start", env)
        assert b.get("success") is True, b
        # already_running path or same pid
        st = _bridge("worker_status", env)
        assert st.get("worker_alive") is True
        assert st.get("worker_pid") == pid1 or b.get("already_running") or b.get("worker_pid") == pid1
    finally:
        _bridge("stop", env)


def test_start_fails_without_strategy():
    path = os.path.join(tempfile.mkdtemp(), "pw4.db")
    env = _env(path, TRADING_STRATEGY="")
    out = _bridge("start", env)
    assert out.get("success") is False
    assert "TRADING_STRATEGY" in out.get("message", "")


def test_kill_stops_worker():
    path = os.path.join(tempfile.mkdtemp(), "pw5.db")
    env = _env(path)
    try:
        assert _bridge("start", env).get("success") is True
        kill = _bridge("kill", env)
        assert kill.get("success") is True
        time.sleep(0.8)
        st = _bridge("status", env)
        assert st.get("kill_switch_active") is True
        assert st.get("worker_alive") is False
    finally:
        _bridge("reset_kill", env)


def test_test_signal_reaches_pipeline_and_sqlite():
    path = os.path.join(tempfile.mkdtemp(), "pw6.db")
    env = _env(path)
    try:
        assert _bridge("start", env).get("success") is True
        inj = _bridge("inject_test_signal", env)
        assert inj.get("success") is True, inj
        # wait for worker loop to pick up signal
        result = None
        for _ in range(20):
            time.sleep(0.5)
            from backend.database.db_manager import DatabaseManager

            db = DatabaseManager(db_path=path)
            raw = db.get_setting("paper_test_signal_result", "")
            if raw:
                result = json.loads(raw)
                break
        assert result is not None, "worker did not process test signal"
        # accepted may be true or false depending on guard, but must have been processed
        assert "accepted" in result or "error" in result
        # If accepted, trade should exist in sqlite
        if result.get("accepted") is True:
            trades = _bridge("trades", env)
            assert trades.get("total_count", 0) >= 1
            positions = _bridge("positions", env)
            assert len(positions.get("positions") or []) >= 1
    finally:
        _bridge("stop", env)
