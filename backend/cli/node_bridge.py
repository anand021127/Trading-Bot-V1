#!/usr/bin/env python3
"""JSON bridge used by server.ts so dashboard actions hit real Python state.

Commands:
  status | start | stop | kill | reset_kill | trades | positions | health
  worker_status | inject_test_signal
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _out(obj: dict) -> None:
    print(json.dumps(obj, default=str))


def _db():
    from backend.database.db_manager import DatabaseManager
    path = os.environ.get("DATABASE_PATH", "data/trading_bot.db")
    return DatabaseManager(db_path=path)


def cmd_status() -> dict:
    from backend.paper.worker_manager import worker_status
    from backend.strategy.trading_engine import BotState

    db = _db()
    BotState._db = db
    w = worker_status()
    st = BotState.status()
    mode = os.environ.get("TRADING_MODE", "paper").lower()
    strategy = os.environ.get("TRADING_STRATEGY", "").strip()
    product = os.environ.get("UPSTOX_ORDER_PRODUCT", "").strip()
    worker_alive = bool(w.get("worker_alive"))
    age = w.get("heartbeat_age_seconds")
    hb_fresh = age is not None and age < 20
    effectively_running = bool(st.get("running")) and worker_alive and hb_fresh
    return {
        "success": True,
        "running": effectively_running,
        "is_running": effectively_running,
        "bot_state_running": bool(st.get("running")),
        "kill_switch_active": bool(st.get("kill_switch_active")),
        "start_time": st.get("start_time"),
        "uptime_seconds": st.get("uptime_seconds", 0),
        "stop_reason": st.get("stop_reason") or "",
        "mode": mode,
        "strategy": strategy or None,
        "order_product": product or None,
        "executor": "PaperTradingRuntime",
        "paper_runtime_ready": mode == "paper"
        and strategy == "V8_D_PULLBACK_ATM"
        and product in ("I", "D"),
        "worker_alive": worker_alive,
        "worker_pid": w.get("worker_pid"),
        "worker_status": w.get("worker_status"),
        "pipeline_ok": w.get("pipeline_ok"),
        "heartbeat_age_seconds": age,
        "last_error": w.get("last_error"),
    }


def cmd_start() -> dict:
    from backend.paper.worker_manager import start_worker
    from backend.strategy.trading_engine import BotState

    mode = os.environ.get("TRADING_MODE", "paper").lower()
    if mode == "live":
        return {"success": False, "message": "Live trading is not enabled."}
    db = _db()
    BotState._db = db
    if BotState.status().get("kill_switch_active"):
        return {"success": False, "message": "Kill switch is active. Reset it first."}

    result = start_worker(wait_seconds=float(os.environ.get("PAPER_WORKER_START_WAIT", "10")))
    if not result.get("success"):
        return result
    return {
        "success": True,
        "message": result.get("message") or "Paper worker started",
        "mode": "paper",
        "executor": "PaperTradingRuntime",
        "worker_pid": result.get("worker_pid") or result.get("spawned_pid"),
        "pipeline_ok": result.get("pipeline_ok"),
        "log_path": result.get("log_path"),
        "already_running": result.get("already_running", False),
    }


def cmd_stop() -> dict:
    from backend.paper.worker_manager import stop_worker
    return stop_worker()


def cmd_kill() -> dict:
    from backend.paper.worker_manager import kill_worker
    return kill_worker()


def cmd_reset_kill() -> dict:
    from backend.strategy.trading_engine import BotState
    from backend.execution.kill_switch import PersistentKillSwitch, KillLevel

    db = _db()
    BotState._db = db
    BotState.reset_kill()
    try:
        PersistentKillSwitch(db).set_level(KillLevel.OFF.value, "dashboard_reset")
    except Exception:
        pass
    return {"success": True, "message": "Kill switch reset."}


def cmd_trades() -> dict:
    db = _db()
    rows = []
    for t in db.list_trades():
        rows.append({
            "id": t.id,
            "symbol": t.symbol,
            "side": t.side,
            "quantity": t.quantity,
            "price": t.price,
            "entry_price": t.price,
            "timestamp": t.timestamp.isoformat() if hasattr(t.timestamp, "isoformat") else str(t.timestamp),
            "strategy": t.strategy,
            "status": t.status,
            "net_pnl": t.pnl,
            "notes": t.notes,
        })
    return {"trades": rows, "total_count": len(rows), "summary": {}}


def cmd_positions() -> dict:
    db = _db()
    rows = []
    for p in db.get_open_positions():
        rows.append({
            "symbol": p.symbol,
            "quantity": p.quantity,
            "average_price": p.average_price,
            "entry_time": p.entry_time.isoformat() if hasattr(p.entry_time, "isoformat") else str(p.entry_time),
            "instrument_key": getattr(p, "instrument_key", "") or "",
            "side": getattr(p, "side", "long"),
            "unrealized_pnl": getattr(p, "unrealized_pnl", 0.0),
        })
    return {"positions": rows}


def cmd_health() -> dict:
    from backend.paper.worker_manager import full_health
    return full_health()


def cmd_worker_status() -> dict:
    from backend.paper.worker_manager import worker_status
    return worker_status()


def cmd_inject_test_signal() -> dict:
    if os.environ.get("PAPER_ALLOW_TEST_SIGNAL", "").strip() not in {"1", "true", "yes"}:
        return {"success": False, "message": "Set PAPER_ALLOW_TEST_SIGNAL=1 to enable test signal injection"}
    db = _db()
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "instrument_key": "NSE_FO|TEST99999",
        "option_type": "CE",
        "underlying": "NIFTY50",
        "strike": 24000,
        "expiry": "2027-01-07",
        "lot_size": 75,
        "premium": 80.0,
        "spot": 24010.0,
        "quantity": 75,
        "stop_loss": 57.6,
        "target": 113.6,
        "atr": 6.0,
        "quote_age_seconds": 1,
    }
    db.save_setting("paper_test_signal_json", json.dumps(payload))
    db.save_setting("paper_test_signal_pending", "true")
    db.save_setting("paper_test_signal_result", "")
    return {"success": True, "message": "Test signal queued for paper worker", "payload": payload}


def main() -> int:
    if len(sys.argv) < 2:
        _out({"success": False, "message": "usage: node_bridge.py <command>"})
        return 2
    cmd = sys.argv[1].strip().lower().replace("-", "_")
    handlers = {
        "status": cmd_status,
        "start": cmd_start,
        "stop": cmd_stop,
        "kill": cmd_kill,
        "reset_kill": cmd_reset_kill,
        "trades": cmd_trades,
        "positions": cmd_positions,
        "health": cmd_health,
        "worker_status": cmd_worker_status,
        "inject_test_signal": cmd_inject_test_signal,
    }
    fn = handlers.get(cmd)
    if not fn:
        _out({"success": False, "message": f"unknown command {cmd}"})
        return 2
    try:
        _out(fn())
        return 0
    except Exception as exc:
        _out({"success": False, "message": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
