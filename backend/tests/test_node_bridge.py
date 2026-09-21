"""Tests for the Node↔Python dashboard bridge."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "backend" / "cli" / "node_bridge.py"


def _run(cmd: str, env: dict) -> dict:
    full = {**os.environ, **env, "PYTHONPATH": str(ROOT)}
    out = subprocess.check_output(
        [sys.executable, str(SCRIPT), cmd],
        env=full,
        cwd=str(ROOT),
        stderr=subprocess.STDOUT,
        timeout=35,
    )
    line = out.decode().strip().splitlines()[-1]
    return json.loads(line)


def _base(path: str, **extra):
    e = {
        "DATABASE_PATH": path,
        "PAPER_WORKER_LOCK": path + ".lock",
        "TRADING_MODE": "paper",
        "TRADING_STRATEGY": "V8_D_PULLBACK_ATM",
        "UPSTOX_ORDER_PRODUCT": "I",
        "PAPER_WORKER_START_WAIT": "12",
        "PAPER_WORKER_INTERVAL_SEC": "1",
    }
    e.update(extra)
    return e


def test_bridge_start_stop_requires_paper_config():
    path = os.path.join(tempfile.mkdtemp(), "bridge.db")
    env = _base(path, TRADING_STRATEGY="", UPSTOX_ORDER_PRODUCT="")
    st = _run("status", env)
    assert st["success"] is True
    assert st["running"] is False

    bad = _run("start", env)
    assert bad["success"] is False
    assert "TRADING_STRATEGY" in bad["message"]

    env = _base(path)
    ok = _run("start", env)
    assert ok["success"] is True, ok
    st2 = _run("status", env)
    assert st2["running"] is True
    assert st2["worker_alive"] is True
    stop = _run("stop", env)
    assert stop["success"] is True


def test_bridge_kill_blocks_and_reset():
    path = os.path.join(tempfile.mkdtemp(), "bridge2.db")
    env = _base(path)
    assert _run("start", env)["success"] is True
    kill = _run("kill", env)
    assert kill["success"] is True
    st = _run("status", env)
    assert st["kill_switch_active"] is True
    assert st["running"] is False
    _run("reset_kill", env)
    st2 = _run("status", env)
    assert st2["kill_switch_active"] is False


def test_bridge_live_start_blocked():
    path = os.path.join(tempfile.mkdtemp(), "bridge3.db")
    env = _base(path, TRADING_MODE="live")
    out = _run("start", env)
    assert out["success"] is False
    assert "not enabled" in out["message"].lower()


def test_bridge_trades_positions_empty():
    path = os.path.join(tempfile.mkdtemp(), "bridge4.db")
    env = {"DATABASE_PATH": path}
    trades = _run("trades", env)
    assert "trades" in trades
    pos = _run("positions", env)
    assert "positions" in pos
