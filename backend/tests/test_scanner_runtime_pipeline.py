"""Scanner/runtime pipeline QA — proves the paper scan loop EXECUTES and that
every iteration is recorded, before anyone may say "V8-D is just selective".

Covers: worker loop alive · scan timestamp/seq updates · scan detail persisted
as VALID json · heartbeat updates · market-data failure surfaced (secrets
redacted) · stale data rejected · NO_SIGNAL persisted with diagnostics · valid
signal reaches paper execution · risk rejection persisted with reason ·
worker exceptions not swallowed · dashboard/Copilot report the REAL state ·
scanner re-arms after a token appears · scan interval scheduling · real
subprocess worker executes scans with no token.

NOTHING here loosens V8-D, enables test signals, or fabricates market data
for the production path: candles/chain below are inputs to the real scanner
code under test (the same approach as test_market_scan_loop.py).
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

from backend.database.db_manager import DatabaseManager
from backend.paper import scan_state as ss
from backend.paper.market_scan_loop import PaperMarketScanner
from backend.strategy.signal import SignalType, StrategySignal
from backend.strategy.strategies.v8d_strategy import V8DStrategy
from backend.strategy.trading_engine import BotState

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parents[2]
TRADING_NOW = datetime(2026, 9, 18, 10, 0, tzinfo=IST)      # Friday, in session
SATURDAY = datetime(2026, 9, 19, 10, 0, tzinfo=IST)


# ── fixtures / fakes ────────────────────────────────────────────────────────
def _candles(now=TRADING_NOW, n=70):
    return [{
        "timestamp": (now - timedelta(minutes=5 * (n - 1 - i))).isoformat(),
        "open": 24000 + i, "high": 24005 + i, "low": 23995 + i,
        "close": 24000 + i, "volume": 1000,
    } for i in range(n)]


def _chain():
    return [
        {"strike": 24050.0, "option_type": "CE", "instrument_key": "NSE_FO|SCAN_CE_TEST",
         "ltp": 85.0, "lot_size": 75, "freeze_quantity": 1800, "option_atr": 5.0,
         "atr": 5.0, "volume": 10000, "oi": 50000},
        {"strike": 24050.0, "option_type": "PE", "instrument_key": "NSE_FO|SCAN_PE_TEST",
         "ltp": 80.0, "lot_size": 75, "freeze_quantity": 1800, "option_atr": 5.0,
         "atr": 5.0, "volume": 8000, "oi": 40000},
    ]


class FakeData:
    def __init__(self, candles=None, chain=None, spot=24050.0, expiry="2027-01-07",
                 candle_exc=None, chain_exc=None):
        self.candles = _candles() if candles is None else candles
        self.chain = _chain() if chain is None else chain
        self.spot, self.expiry = spot, expiry
        self.candle_exc, self.chain_exc = candle_exc, chain_exc
        self.calls = 0

    def get_current_candles(self, symbol, interval="5minute", limit=120):
        self.calls += 1
        if self.candle_exc:
            raise self.candle_exc
        return list(self.candles)

    def get_nearest_expiry(self, symbol):
        return self.expiry

    def get_option_chain_with_spot(self, symbol, expiry_date):
        if self.chain_exc:
            raise self.chain_exc
        return list(self.chain), self.spot


class BuyStrategy:
    """Deterministic BUY used ONLY to prove signal → risk → paper execution
    wiring (identical to test_market_scan_loop.MockStrategy)."""
    name = "V8_D_PULLBACK_ATM"

    def evaluate_v8d_signal(self, **kw):
        sig = StrategySignal(strategy_name=self.name, symbol=kw["underlying_symbol"],
                             signal=SignalType.BUY, entry_price=85.0, stop_loss=61.2,
                             target=120.0, generated_at=datetime.now(timezone.utc).isoformat())
        sig.indicators = {
            "selected_contract": {"instrument_key": "NSE_FO|SCAN_CE_TEST", "option_type": "CE",
                                  "strike": 24000.0, "lot_size": 75, "freeze_quantity": 1800,
                                  "ltp": 85.0, "option_atr": 5.0, "atr": 5.0},
            "sizing": {"quantity": 75}, "underlying_spot": kw["spot_price"],
            "lot_size": 75, "option_type": "CE", "atm_strike": 24000,
        }
        return sig, type("L", (), {"decision": "ACCEPTED"})()


class FixedNowScanner(PaperMarketScanner):
    """Real scanner; only the wall clock is pinned to a known trading day."""
    fixed_now = TRADING_NOW

    def scan_once(self, runtime, **kw):
        kw.setdefault("now", self.fixed_now)
        return super().scan_once(runtime, **kw)


class ExplodingScanner:
    underlying = "NIFTY50"

    def scan_once(self, *a, **k):
        raise RuntimeError("boom Bearer abcdef0123456789abcdef access_token=SECRETVALUE123")


@pytest.fixture()
def worker(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(), "scanrt.db")
    for k, v in {"DATABASE_PATH": path, "TRADING_MODE": "paper",
                 "TRADING_STRATEGY": "V8_D_PULLBACK_ATM", "UPSTOX_ORDER_PRODUCT": "I",
                 "RISK_PER_TRADE_PCT": "0.025", "TRADING_CAPITAL": "100000",
                 "PAPER_WORKER_LOCK": path + ".lock"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PAPER_SCAN_INTERVAL_SEC", "1")
    from backend.paper.paper_runtime import PaperTradingRuntime
    from backend.paper.paper_worker import PaperWorker
    w = PaperWorker()
    w.runtime = PaperTradingRuntime(db=w.db)
    w.runtime.now_fn = lambda: TRADING_NOW
    w._scan_interval = 0.0          # every _maybe_scan call is "due"
    BotState._db = w.db
    BotState.start()
    yield w
    w._hb_stop.set()
    try:
        w.db.close()
    except Exception:
        pass


def _scan(w, scanner=None):
    w.scanner = scanner
    w._next_scan_mono = 0.0
    w._maybe_scan()
    return ss.read_scan_record(w.db)


# ── 1-3: loop executes, timestamps/seq advance, detail is valid JSON ────────
def test_worker_tick_executes_scan_and_updates_seq_timestamp_heartbeat(worker):
    worker.scanner = FixedNowScanner(data=FakeData(), strategy=V8DStrategy(), min_bars=60)
    worker._tick()
    r1 = ss.read_scan_record(worker.db)
    hb1 = worker.db.get_setting(ss.HB_KEY)
    time.sleep(0.02)
    worker._next_scan_mono = 0.0
    worker._tick()
    r2 = ss.read_scan_record(worker.db)
    assert r1 and r2, "no scan record persisted by the worker tick"
    assert r2["seq"] == r1["seq"] + 1
    assert r2["recorded_at"] > r1["recorded_at"]
    assert worker.db.get_setting(ss.HB_KEY) >= hb1
    assert int(worker.db.get_setting(ss.LOOP_KEY)) == 2
    assert worker.db.get_setting(ss.SCAN_TS_KEY) == r2["recorded_at"]
    json.loads(worker.db.get_setting(ss.SCAN_DETAIL_KEY))     # valid JSON
    assert len(ss.read_scan_history(worker.db)) == 2


def test_scan_interval_is_scheduled_not_every_tick(worker):
    worker._scan_interval = 3600.0
    worker.scanner = FixedNowScanner(data=FakeData(), strategy=V8DStrategy(), min_bars=60)
    worker._next_scan_mono = 0.0
    worker._maybe_scan()
    worker._maybe_scan()
    worker._maybe_scan()
    assert int(worker.db.get_setting(ss.SCAN_SEQ_KEY)) == 1


def test_persisted_record_is_always_valid_json_even_when_huge():
    big = {"ai_reason_codes": ["X" * 500] * 40, "rejection": ["Y" * 500] * 40,
           "selected_contract": {f"k{i}": "Z" * 400 for i in range(30)}}
    rec = ss.build_scan_record(seq=1, scanned=True, traded=False, reason="no_trade:NONE",
                               details=big, error="E" * 5000)
    txt = ss._bounded_json(rec)
    assert len(txt) <= ss.MAX_RECORD_CHARS
    parsed = json.loads(txt)
    assert parsed["seq"] == 1 and parsed["reason"] == "no_trade:NONE"


# ── 8: NO_SIGNAL is a persisted, explained strategy outcome ─────────────────
def test_real_v8d_no_signal_is_persisted_with_full_diagnostics(worker):
    scanner = FixedNowScanner(data=FakeData(), strategy=V8DStrategy(), min_bars=60)
    rec = _scan(worker, scanner)
    assert rec["scanned"] is True and rec["traded"] is False
    assert rec["category"] in ("NO_SIGNAL", "SIGNAL"), rec["reason"]
    assert rec["reason"].startswith("no_trade:"), rec["reason"]
    assert rec["strategy"] == "V8_D_PULLBACK_ATM" and rec["underlying"] == "NIFTY50"
    assert rec["candle_count"] == 70
    assert rec["expiry"] == "2027-01-07"
    assert rec["option_chain_count"] == 2
    assert rec["details"]["v8d_evaluated"] is True
    assert rec["data_status"] == "OK"
    assert rec["session_status"] == "OPEN"
    assert rec["last_candle_ts"] and rec["candle_age_seconds"] is not None
    assert rec["risk_decision"] == "NOT_EVALUATED" and rec["execution_decision"] == "NOT_ATTEMPTED"
    assert "V8-D evaluated successfully" in ss.summarize_record(rec)
    st = ss.compute_runtime_state(worker.db, pid_alive=lambda p: True)
    # heartbeat not written in this unit test -> only assert classification path
    assert st["last_scan"]["reason"] == rec["reason"] or st["state"] in (
        ss.STARTING, ss.WORKER_NOT_RESPONDING)


# ── 9-10: valid signal → risk → paper execution; rejection has a reason ─────
def test_valid_signal_reaches_paper_execution_and_persists(worker):
    rec = _scan(worker, FixedNowScanner(data=FakeData(), strategy=BuyStrategy()))
    assert rec["traded"] is True, rec
    assert rec["signal"] == "BUY"
    assert rec["risk_decision"] == "PASSED" and rec["execution_decision"] == "FILLED_PAPER"
    assert len(worker.db.list_trades()) >= 1 and len(worker.db.get_open_positions()) >= 1
    assert worker.runtime.trades_today == 1
    assert worker.db.get_setting("paper_trades_today") == "1"


def test_risk_rejection_is_persisted_with_exact_reason(worker):
    from backend.execution.kill_switch import FULL_SYSTEM_STOP
    worker.runtime.kill.set_level(FULL_SYSTEM_STOP, "test")
    rec = _scan(worker, FixedNowScanner(data=FakeData(), strategy=BuyStrategy()))
    assert rec["traded"] is False and rec["signal"] == "BUY"
    assert rec["reason"].startswith("rejected:"), rec["reason"]
    assert rec["execution_decision"].startswith("REJECTED:")
    assert not worker.db.list_trades()
    from backend.copilot.gate_chain import build_gate_chain_from_db
    chain = build_gate_chain_from_db(worker.db)
    assert chain["available"] and chain["stage"] != "TRADED"
    assert chain["diagnostics"]["execution_decision"].startswith("REJECTED:")


def test_second_day_is_not_blocked_by_lifetime_trade_counter(worker):
    """Regression: `paper_trades_today` used to be a LIFETIME counter."""
    worker.db.save_setting("paper_trades_today", "999")
    rec = _scan(worker, FixedNowScanner(data=FakeData(), strategy=BuyStrategy()))
    assert rec["traded"] is True, rec["reason"]


# ── 6-7: market-data failure surfaced (redacted), stale data rejected ───────
def test_market_data_failure_is_surfaced_and_secrets_redacted(worker):
    exc = RuntimeError("401 Unauthorized Bearer abcdef0123456789abcdef token=TOPSECRET123")
    rec = _scan(worker, FixedNowScanner(data=FakeData(candle_exc=exc), strategy=V8DStrategy()))
    assert rec["scanned"] is True and rec["traded"] is False
    assert rec["reason"] == "candle_fetch_error:RuntimeError"
    assert rec["category"] == "DATA_ERROR" and rec["data_status"] == "ERROR"
    blob = worker.db.get_setting(ss.SCAN_DETAIL_KEY)
    assert "abcdef0123456789abcdef" not in blob and "TOPSECRET123" not in blob
    assert rec["error"] and "RuntimeError" in rec["error"]
    _fresh_hb(worker.db, pid=os.getpid())
    st = ss.compute_runtime_state(worker.db, pid_alive=lambda p: True)
    assert st["state"] == ss.RUNNING_DATA_ERROR
    assert "market data is not usable" in st["summary"]


def test_option_chain_failure_is_surfaced(worker):
    rec = _scan(worker, FixedNowScanner(
        data=FakeData(chain_exc=ConnectionError("chain down")), strategy=V8DStrategy()))
    assert rec["reason"] == "chain_fetch_error:ConnectionError"
    assert rec["category"] == "DATA_ERROR" and rec["expiry"] == "2027-01-07"


def test_stale_candles_rejected_safely(worker):
    stale = _candles(now=TRADING_NOW - timedelta(hours=5))
    rec = _scan(worker, FixedNowScanner(data=FakeData(candles=stale), strategy=BuyStrategy(),
                                        max_candle_age_seconds=600))
    assert rec["traded"] is False and rec["reason"].startswith("stale_candles")
    assert rec["category"] == "DATA_ERROR"
    assert rec["data_status"] == "STALE_OR_INSUFFICIENT"
    assert not worker.db.list_trades()


# ── 11: worker exceptions are not silently swallowed ────────────────────────
def test_scan_exception_is_persisted_and_visible_not_swallowed(worker):
    rec = _scan(worker, ExplodingScanner())
    assert rec["reason"] == "scan_error:RuntimeError" and rec["category"] == "SCANNER_ERROR"
    assert "SECRETVALUE123" not in worker.db.get_setting(ss.SCAN_DETAIL_KEY)
    assert "abcdef0123456789abcdef" not in (worker.db.get_setting(ss.ERR_KEY) or "")
    assert "RuntimeError" in worker.db.get_setting(ss.ERR_KEY)
    worker._write_hb("running")
    st = ss.compute_runtime_state(worker.db, pid_alive=lambda p: True)
    assert st["state"] == ss.RUNNING_SCANNER_ERROR
    # and the loop keeps going afterwards
    rec2 = _scan(worker, FixedNowScanner(data=FakeData(), strategy=V8DStrategy()))
    assert rec2["seq"] == rec["seq"] + 1


# ── market closed vs scanner not executing ──────────────────────────────────
def test_market_closed_is_recorded_as_waiting_not_as_broken(worker):
    class Sat(FixedNowScanner):
        fixed_now = SATURDAY
    rec = _scan(worker, Sat(data=FakeData(), strategy=V8DStrategy()))
    assert rec["reason"] == "market_closed" and rec["category"] == "MARKET_CLOSED"
    worker._write_hb("running")
    st = ss.compute_runtime_state(worker.db, pid_alive=lambda p: True)
    assert st["state"] == ss.RUNNING_WAITING_FOR_MARKET
    assert "loop itself is executing" in st["summary"]


# ── scanner not armed (no token): recorded every iteration, then re-armed ───
def test_no_token_records_scanner_disabled_every_iteration(worker):
    worker.db.save_setting(ss.MARKET_SCAN_KEY, "disabled_no_token")
    with mock.patch.object(worker, "_init_market_scanner", lambda: None):
        worker._last_scanner_init_mono = time.monotonic()      # suppress retry
        r1 = _scan(worker, None)
        r2 = _scan(worker, None)
    assert r1["reason"].startswith("scanner_disabled") and r2["seq"] == r1["seq"] + 1
    assert r1["data_status"] == "NO_MARKET_DATA_SOURCE" and r1["category"] == "DATA_ERROR"
    worker._write_hb("running")
    st = ss.compute_runtime_state(worker.db, pid_alive=lambda p: True)
    assert st["state"] == ss.RUNNING_DATA_ERROR


def test_scanner_is_rearmed_when_token_appears_after_worker_start(worker):
    armed = FixedNowScanner(data=FakeData(), strategy=V8DStrategy(), min_bars=60)

    def fake_init():
        worker.scanner = armed
    worker.scanner = None
    worker._last_scanner_init_mono = -10_000.0
    worker._next_scan_mono = 0.0
    with mock.patch.object(worker, "_init_market_scanner", fake_init):
        worker._maybe_scan()
    rec = ss.read_scan_record(worker.db)
    assert worker.scanner is armed
    assert rec["reason"].startswith("no_trade:"), rec["reason"]


# ── 12-13: dashboard API + Copilot use the REAL persisted state ─────────────
def _fresh_hb(db, pid=4242, status="running"):
    now = datetime.now(timezone.utc).isoformat()
    db.save_setting(ss.HB_KEY, now)
    db.save_setting(ss.PID_KEY, str(pid))
    db.save_setting(ss.STATUS_KEY, status)


def test_runtime_state_machine_never_reports_running_from_flag_alone(worker):
    db = worker.db
    # flag says running, but no worker ever existed and start was long ago
    db.save_setting("bot_state_start_time",
                    (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat())
    st = ss.compute_runtime_state(db, pid_alive=lambda p: False)
    assert st["state"] == ss.WORKER_NOT_RESPONDING and not st["state"].startswith("RUNNING")
    # just started -> STARTING
    db.save_setting("bot_state_start_time", datetime.now(timezone.utc).isoformat())
    assert ss.compute_runtime_state(db, pid_alive=lambda p: False)["state"] == ss.STARTING
    # worker up + heartbeat, but NO scan record long after start -> scanner error
    _fresh_hb(db)
    db.save_setting("bot_state_start_time",
                    (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat())
    st = ss.compute_runtime_state(db, pid_alive=lambda p: True)
    assert st["state"] == ss.RUNNING_SCANNER_ERROR and "NO scan has been recorded" in st["summary"]
    # stalled scan loop: fresh heartbeat but old record
    old = datetime.now(timezone.utc) - timedelta(minutes=4)
    rec = ss.build_scan_record(seq=3, scanned=True, traded=False, reason="no_trade:NONE", now=old)
    ss.persist_scan_record(db, rec)
    st = ss.compute_runtime_state(db, pid_alive=lambda p: True)
    assert st["state"] == ss.RUNNING_SCANNER_ERROR and "stalled" in st["summary"]
    # healthy no-signal
    rec = ss.build_scan_record(seq=4, scanned=True, traded=False, reason="no_trade:NONE",
                               details={"rejection": ["pullback not met"]})
    ss.persist_scan_record(db, rec)
    st = ss.compute_runtime_state(db, pid_alive=lambda p: True)
    assert st["state"] == ss.RUNNING_NO_SIGNAL and "pullback not met" in st["summary"]
    # stopped / killed
    BotState.stop("test")
    assert ss.compute_runtime_state(db, pid_alive=lambda p: True)["state"] == ss.STOPPED
    BotState.start()
    BotState.kill("test")
    assert ss.compute_runtime_state(db, pid_alive=lambda p: True)["kill_switch_active"] is True
    BotState.reset_kill()


def test_dashboard_bot_status_api_reports_real_scanner_state(worker):
    from backend.api.routers import bot_control
    db = worker.db
    BotState.start()
    db.save_setting("bot_state_start_time",
                    (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat())
    out = asyncio.run(bot_control.bot_status())
    assert out["running"] is True                       # the FLAG
    assert out["runtime_state"] == ss.WORKER_NOT_RESPONDING   # the TRUTH
    assert out["effective_running"] is False
    _fresh_hb(db, pid=os.getpid())
    ss.persist_scan_record(db, ss.build_scan_record(
        seq=9, scanned=True, traded=False, reason="no_trade:NONE"))
    out = asyncio.run(bot_control.bot_status())
    assert out["runtime_state"] == ss.RUNNING_NO_SIGNAL and out["effective_running"] is True


def test_copilot_uses_actual_latest_scan_state(worker):
    from backend.copilot import full_context
    from backend.copilot.gate_chain import build_gate_chain_from_db
    db = worker.db
    empty = build_gate_chain_from_db(db)
    assert empty["available"] is False and "Current runtime state" in empty["reason"]
    _fresh_hb(db, pid=os.getpid())
    rec = _scan(worker, FixedNowScanner(data=FakeData(), strategy=V8DStrategy(), min_bars=60))
    chain = build_gate_chain_from_db(db)
    assert chain["available"] is True and chain["scanned"] is True
    assert chain["diagnostics"]["candle_count"] == 70
    assert chain["diagnostics"]["option_chain_count"] == 2
    assert "evaluated successfully" in chain["human_summary"]
    assert chain["gates"]["data"]["status"] == "OK"
    assert chain["gates"]["v8d_signal"]["status"] == "NO_SIGNAL"      # NO_SIGNAL is never called a rejected signal
    scanner = full_context._scanner(None, db)
    assert scanner["available"] is True
    assert scanner["scan_seq"] == rec["seq"]
    assert scanner["worker_last_scan_reason"] == rec["reason"]
    assert scanner["candle_count"] == 70


def test_data_failure_gate_chain_says_data_problem_not_strategy(worker):
    from backend.copilot.gate_chain import build_gate_chain_from_db
    _scan(worker, FixedNowScanner(data=FakeData(candle_exc=RuntimeError("x")),
                                  strategy=V8DStrategy()))
    chain = build_gate_chain_from_db(worker.db)
    assert chain["gates"]["data"]["status"] == "REJECTED"
    assert chain["gates"]["v8d_signal"]["status"] == "NOT_EVALUATED"
    assert "not evaluated" in chain["human_summary"].lower() or "data" in chain["human_summary"].lower()


# ── watchdog / start guarantees ─────────────────────────────────────────────
def test_watchdog_respawns_dead_worker_with_backoff(monkeypatch, worker):
    from backend.paper import worker_manager as wm
    wm._WATCHDOG.update({"attempts": 0, "last_attempt": 0.0})
    monkeypatch.setattr(wm, "worker_status", lambda: {
        "bot_state": {"running": True, "kill_switch_active": False},
        "worker_alive": False, "heartbeat_age_seconds": None, "last_error": None})
    calls = []
    monkeypatch.setattr(wm, "ensure_worker_running",
                        lambda *a, **k: calls.append(1) or {"success": True, "message": "ok"})
    assert wm.watchdog_check(now_mono=1000.0)["action"] == "respawn"
    assert wm.watchdog_check(now_mono=1010.0)["action"] == "backoff"
    assert wm.watchdog_check(now_mono=1100.0)["action"] == "respawn"
    assert len(calls) == 2
    monkeypatch.setattr(wm, "worker_status", lambda: {
        "bot_state": {"running": True, "kill_switch_active": True},
        "worker_alive": False, "heartbeat_age_seconds": None})
    assert wm.watchdog_check(now_mono=9999.0)["action"] == "none"   # never with kill switch


def test_api_start_spawns_worker_or_fails_honestly(monkeypatch, worker):
    from backend.api.routers import bot_control
    from backend.paper import worker_manager as wm
    from types import SimpleNamespace
    monkeypatch.setenv("PAPER_WORKER_AUTOSPAWN", "1")
    monkeypatch.setattr(bot_control, "_paper_runtime_from_app", lambda: SimpleNamespace(db=worker.db))
    monkeypatch.setattr(bot_control, "_settings_now", lambda: SimpleNamespace(mode="paper"))
    BotState.stop("t")
    monkeypatch.setattr(wm, "ensure_worker_running",
                        lambda *a, **k: {"success": False, "message": "boom", "last_error": "x"})
    out = asyncio.run(bot_control.start_bot())
    assert out["success"] is False and "NOT running" in out["message"]
    assert BotState.is_running() is False            # flag rolled back, no phantom RUNNING
    monkeypatch.setattr(wm, "ensure_worker_running",
                        lambda *a, **k: {"success": True, "message": "spawned", "worker_pid": 77})
    out = asyncio.run(bot_control.start_bot())
    assert out["success"] is True and out["worker_pid"] == 77


# ── safety: no order placement anywhere in the paper scan path ──────────────
def test_paper_scan_path_contains_no_live_order_calls():
    banned = ("place_order", "/order/place", "place_multi_order", "LiveBroker(")
    for rel in ("backend/paper/paper_worker.py", "backend/paper/market_scan_loop.py",
                "backend/paper/scan_state.py", "backend/paper/worker_manager.py"):
        text = (ROOT / rel).read_text()
        for b in banned:
            assert b not in text, f"{rel} references {b}"


def test_v8d_parameters_unchanged():
    s = V8DStrategy()
    assert s.max_account_risk_pct == 0.025
    assert s.max_daily_trades >= 1
    assert (ROOT / "backend/strategy/strategies/v8d_strategy.py").read_text().count("V8_D_PULLBACK_ATM") >= 1


# ── real subprocess: worker actually executes scan iterations ───────────────
def _bridge(cmd, env):
    out = subprocess.check_output([sys.executable, str(ROOT / "backend/cli/node_bridge.py"), cmd],
                                  env=env, cwd=str(ROOT), timeout=90)
    return json.loads(out.decode().strip().splitlines()[-1])


def test_real_worker_process_executes_and_records_scans_without_token():
    path = os.path.join(tempfile.mkdtemp(), "proc.db")
    env = {**os.environ, "PYTHONPATH": str(ROOT), "DATABASE_PATH": path,
           "PAPER_WORKER_LOCK": path + ".lock", "TRADING_MODE": "paper",
           "TRADING_STRATEGY": "V8_D_PULLBACK_ATM", "UPSTOX_ORDER_PRODUCT": "I",
           "TRADING_CAPITAL": "100000", "RISK_PER_TRADE_PCT": "0.025",
           "MAX_ALLOCATION_PCT": "0.18", "PAPER_WORKER_INTERVAL_SEC": "0.5",
           "PAPER_SCAN_INTERVAL_SEC": "1", "PAPER_WORKER_START_WAIT": "15",
           "PAPER_ALLOW_TEST_SIGNAL": "0", "UPSTOX_ACCESS_TOKEN": "",
           "ALLOW_LIVE_UPSTOX": "0"}
    try:
        started = _bridge("start", env)
        assert started.get("success") is True, started
        db = DatabaseManager(db_path=path)
        seqs, hbs, loops = set(), set(), set()
        deadline = time.time() + 20
        while time.time() < deadline and len(seqs) < 3:
            time.sleep(0.7)
            rec = ss.read_scan_record(db)
            if rec:
                seqs.add(rec["seq"])
            hbs.add(db.get_setting(ss.HB_KEY))
            loops.add(db.get_setting(ss.LOOP_KEY))
        st = _bridge("status", env)
        rec = ss.read_scan_record(db)
        db.close()
        assert len(seqs) >= 3, f"scan seq did not advance: {seqs}"
        assert len(hbs) >= 2 and len(loops) >= 2
        assert rec["reason"].startswith("scanner_disabled") or rec["reason"] in (
            "market_closed",) or rec["reason"].startswith(("entry_window_closed", "no_trade")) \
            or rec["reason"].startswith(("candle_fetch_error", "stale", "no_candles", "insufficient"))
        assert st["worker_alive"] is True and st["runtime_state"] in (
            ss.RUNNING_DATA_ERROR, ss.RUNNING_WAITING_FOR_MARKET, ss.RUNNING_NO_SIGNAL,
            ss.RUNNING_SCANNING), st
        assert st["runtime_state"] != ss.RUNNING_SCANNER_ERROR
    finally:
        _bridge("stop", env)
