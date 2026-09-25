"""Production-hardening regression tests.

Every test here maps to a real production failure mode found during the
engineering audit (restart risk reset, lost exit-risk state, fake exit
endpoint, intraday paper exits never firing, offline live-order reachability,
non-atomic intents). These run fully offline: no Upstox call can be made.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.database.db_manager import DatabaseManager
from backend.database.models import Position, Trade
from backend.execution.eod import is_past_square_off
from backend.paper.paper_broker import PaperBroker
from backend.paper.worker_lock import pid_is_alive


def _db():
    return DatabaseManager(db_path=os.path.join(
        tempfile.mkdtemp(prefix="hardreg_"), f"t_{uuid.uuid4().hex}.db"))


def _make_runtime(db, broker=None, **env_over):
    """Build a PaperTradingRuntime on a temp DB with offline env defaults."""
    env = {
        "TRADING_MODE": "paper",
        "TRADING_STRATEGY": "V8_D_PULLBACK_ATM",
        "UPSTOX_ORDER_PRODUCT": "I",
        "TRADING_CAPITAL": "100000",
        "RISK_PER_TRADE_PCT": "0.025",
        "MAX_DAILY_LOSS_PCT": "0.02",
        "MAX_TRADES_PER_DAY": "3",
        "MAX_CONCURRENT_POSITIONS": "1",
        "EOD_SQUARE_OFF": "15:15",
        "TRADING_BOT_OFFLINE_TESTS": "1",
    }
    env.update(env_over)
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        from backend.paper.paper_runtime import PaperTradingRuntime
        rt = PaperTradingRuntime(db=db, broker=broker or PaperBroker())
        yield rt
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _submit(rt, signal=None):
    """Submit an entry as if it were mid-morning (tests may run after the
    15:15 IST square-off cutoff, which would otherwise refuse entries)."""
    morning = rt.now_fn().replace(hour=10, minute=0, second=0, microsecond=0)
    with mock.patch.object(rt, "now_fn", return_value=morning):
        return rt.submit_entry(dict(signal or _ENTRY_SIGNAL))


_ENTRY_SIGNAL = {
    "timestamp": "2026-09-24T10:00:00+00:00",
    "instrument_key": "NSE_FO|69780",
    "option_type": "CE",
    "underlying": "NIFTY50",
    "strike": 24800.0,
    "expiry": "2027-01-07",
    "lot_size": 75,
    "premium": 120.0,      # 75 × 120 = ₹9,000 notional (≤ 18% of ₹1,00,000)
    "spot": 24810.0,
    "quantity": 75,
    "stop_loss": 105.0,    # max loss (120-105)×75 = ₹1,125 ≤ 2.5% risk cap
    "target": 168.0,
    "atr": 6.0,
    "quote_age_seconds": 1,
    "side": "BUY",
    "strategy": "V8_D_PULLBACK_ATM",
}


# ─── 1. Restart risk-state recovery ─────────────────────────────────────────

def test_restart_restores_realized_equity_and_daily_counters():
    """A loss followed by a restart must NOT reset equity to starting capital
    or zero trades_today (restart could otherwise bypass daily limits)."""
    db = _db()
    rt = next(_make_runtime(db))
    # Simulate a realized loss and 2 trades taken today (durable counters).
    db.add_daily_trades(rt.now_fn().date().isoformat(), 2)
    db.add_daily_realized_pnl(rt.now_fn().date().isoformat(), -300.0)
    rt.realized_equity = 99700.0
    rt.realized_pnl_total = -300.0
    rt.trades_today = 2
    rt.daily_realized_pnl = -300.0
    rt._persist_day_state()
    db.close()

    # Fresh runtime = worker restart
    db2 = DatabaseManager(db_path=db.db_path)
    rt2 = next(_make_runtime(db2))
    assert rt2.realized_equity == 99700.0
    assert rt2.trades_today == 2
    assert rt2.daily_realized_pnl == -300.0
    db2.close()


def test_restart_new_day_resets_daily_counters_but_keeps_equity():
    db = _db()
    rt = next(_make_runtime(db))
    yday = (rt.now_fn().date() - timedelta(days=1)).isoformat()
    # Simulate: yesterday the daily trade limit was reached, then the process
    # stayed down overnight.
    db.add_daily_trades(yday, 3)
    rt._trade_day = yday
    rt.realized_equity = 99500.0
    rt.trades_today = 3
    rt.daily_realized_pnl = -120.0
    rt._persist_day_state()
    db.close()

    db2 = DatabaseManager(db_path=db.db_path)
    rt2 = next(_make_runtime(db2))  # restart on the new day
    assert rt2.trades_today == 0, "new day must reset the trade counter"
    assert rt2.daily_realized_pnl == 0.0
    assert rt2.realized_equity == 99500.0, "equity carries across days"
    db2.close()


def test_restart_restores_position_risk_state_from_extra():
    """SL/target/lot/trade_id must survive restart via positions.extra."""
    db = _db()
    rt = next(_make_runtime(db))
    res = _submit(rt)
    assert getattr(res, "accepted", False), getattr(res, "reason", None)
    db.close()

    db2 = DatabaseManager(db_path=db.db_path)
    rt2 = next(_make_runtime(db2))
    pos = rt2.broker.positions.get("NSE_FO|69780")
    assert pos is not None, "position not hydrated after restart"
    assert int(pos["quantity"]) == 75
    assert pos["stop_loss"] == 105.0
    assert pos["target"] == 168.0
    assert pos["lot_size"] == 75
    assert pos["trade_id"], "trade_id lost across restart"
    db2.close()


def test_exit_after_restart_uses_restored_stop_and_persists_once():
    """Crash after entry, restart, then SL hit → one correct exit, correct P&L."""
    db = _db()
    rt = next(_make_runtime(db))
    res = _submit(rt)
    assert getattr(res, "accepted", False)
    db.close()

    db2 = DatabaseManager(db_path=db.db_path)
    rt2 = next(_make_runtime(db2))
    exit_summary = rt2.on_option_quote("NSE_FO|69780", 100.0)  # below restored SL (105)
    assert exit_summary is not None, "SL exit did not fire after restart"
    assert exit_summary["exit_reason"] == "STOP_LOSS"
    assert exit_summary["quantity"] == 75
    # No double exit
    assert rt2.on_option_quote("NSE_FO|69780", 220.0) is None
    # Ledger closed
    assert db2.get_open_positions() == []
    db2.close()


# ─── 2. Manual exit queue (real endpoint behavior) ──────────────────────────

def test_manual_exit_queue_deduplicates_and_executes_once():
    db = _db()
    rt = next(_make_runtime(db))
    assert _submit(rt).accepted
    first = rt.queue_manual_exit("NSE_FO|69780")
    assert first["queued"] is True
    dup = rt.queue_manual_exit("NSE_FO|69780")
    assert dup["queued"] is False and dup["reason"] == "exit_already_pending"
    drained = rt.drain_manual_exit_queue()
    assert drained == 1
    assert db.get_open_positions() == []
    # Queue drained; second drain is a no-op; exit is idempotent
    assert rt.drain_manual_exit_queue() == 0
    assert rt.queue_manual_exit("NSE_FO|69780")["queued"] is True  # can queue again safely
    assert rt.drain_manual_exit_queue() == 0  # position already closed → no-op
    db.close()


def test_manual_exit_for_unknown_position_is_safe_noop():
    db = _db()
    rt = next(_make_runtime(db))
    rt.queue_manual_exit("NSE_FO|000000")
    assert rt.drain_manual_exit_queue() == 0
    db.close()


def test_trading_router_exit_endpoint_queues_when_runtime_attached():
    """The router must actually queue an exit when a real runtime is attached
    (regression: it used to return exit_queued while doing nothing)."""
    from backend.api.routers import bot_control, trading
    from backend.api.routers.trading import manual_exit_position

    db = _db()
    rt = next(_make_runtime(db))
    assert _submit(rt).accepted
    token = mock.patch.object(bot_control, "get_paper_runtime", return_value=rt)
    with token:
        resp = asyncio.run(manual_exit_position("NSE_FO|69780"))
    assert resp["status"] == "exit_queued", resp
    drained = rt.drain_manual_exit_queue()
    assert drained == 1
    assert db.get_open_positions() == []
    db.close()


# ─── 3. Offline live-order guards ────────────────────────────────────────────

def test_paper_place_never_reaches_real_upstox_place_order():
    """Paper execution path must be structurally incapable of calling the real
    Upstox order-placement endpoint."""
    import backend.broker.upstox_client as uc

    db = _db()
    rt = next(_make_runtime(db))
    called = {"n": 0}

    def _explode(*a, **k):
        called["n"] += 1
        raise AssertionError("REAL UPSTOX place_order CALLED FROM PAPER PATH")

    with mock.patch.object(uc.UpstoxClient, "place_order", side_effect=_explode), \
         mock.patch.object(uc.requests.Session, "post", side_effect=_explode), \
         mock.patch.object(uc.requests.Session, "request", side_effect=_explode):
        res = _submit(rt)
        assert getattr(res, "accepted", False), getattr(res, "reason", None)
        # Intraday exit path too
        ex = rt.on_option_quote("NSE_FO|69780", 300.0)
        assert ex is not None
    assert called["n"] == 0
    db.close()


def test_offline_guard_blocks_real_upstox_place_order_even_when_called_directly():
    import backend.broker.upstox_client as uc

    client = uc.UpstoxClient(access_token="x" * 40)
    with pytest.raises(uc.UpstoxAPIError) as ei:
        client.place_order(
            symbol="NIFTY50", transaction_type="BUY", quantity=75,
            product="I", instrument_key="NSE_FO|69780",
        )
    assert "ORDER" in str(ei.value) and "offline" in str(ei.value).lower()
    # requests.Session.post must never even be constructed with an order payload
    with mock.patch.object(uc.requests.Session, "post", side_effect=AssertionError("network write attempted")):
        with pytest.raises(uc.UpstoxAPIError):
            client.place_order(
                symbol="NIFTY50", transaction_type="BUY", quantity=75,
                product="I", instrument_key="NSE_FO|69780",
            )


# ─── 4. Idempotency / intent durability ──────────────────────────────────────

def test_order_intent_insert_is_idempotent_under_retry():
    db = _db()
    payload = {"signal": {"k": "v"}}
    db.save_order_intent("sig-1", payload)
    db.save_order_intent("sig-1", payload, status="SUBMITTED")  # retry attempt
    row = db.get_order_intent("sig-1")
    assert row["status"] == "INTENT", "a retry overwrote the original intent"
    # broker_order_id can never be cleared once set
    db.update_order_intent("sig-1", broker_order_id="ORD123")
    db.update_order_intent("sig-1", broker_order_id=None, status="RETRY")
    row = db.get_order_intent("sig-1")
    assert row["broker_order_id"] == "ORD123", "broker_order_id was cleared"
    assert row["status"] == "RETRY"
    db.close()


def test_duplicate_signal_100x_never_creates_two_positions():
    db = _db()
    rt = next(_make_runtime(db))
    res = _submit(rt)
    assert getattr(res, "accepted", False)
    dup_count = 0
    for _ in range(100):
        r = rt.submit_entry(dict(_ENTRY_SIGNAL))
        if not getattr(r, "accepted", True):
            dup_count += 1
    assert dup_count == 100, f"only {dup_count}/100 duplicates were blocked"
    positions = db.get_open_positions()
    assert len(positions) == 1
    assert int(positions[0].quantity) == 75
    db.close()


# ─── 5. Database hardening ───────────────────────────────────────────────────

def test_sqlite_wal_and_busy_timeout_applied():
    db = _db()
    conn = db._connect()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert str(mode).lower() == "wal"
    assert int(timeout) >= 5000
    db.close()


def test_close_is_idempotent_and_releases_file():
    db = _db()
    db.get_setting("x")
    db.close()
    db.close()  # second close is a no-op
    # File can be removed (Windows: open handles block deletion)
    os.remove(db.db_path)


def test_daily_counters_atomic_increment():
    db = _db()
    for i in range(5):
        assert db.add_daily_trades("2026-09-24", 1) == i + 1  # 1..5 sequential
    assert db.add_daily_trades("2026-09-24", 1) == 6
    db.add_daily_realized_pnl("2026-09-24", -10.5)
    db.add_daily_realized_pnl("2026-09-24", -4.5)
    assert db.get_daily_counters("2026-09-24")["realized_pnl"] == -15.0
    assert db.get_daily_counters("2026-09-25")["trades_taken"] == 0
    db.close()


# ─── 6. Cross-platform worker liveness ──────────────────────────────────────

def test_pid_liveness_current_process_true():
    assert pid_is_alive(os.getpid()) is True


def test_pid_liveness_bogus_and_invalid():
    assert pid_is_alive(-1) is False
    assert pid_is_alive(0) is False
    # PID 999999 is virtually guaranteed not to exist in any test environment
    assert pid_is_alive(999999) is False


def test_worker_status_reports_spawned_worker_alive():
    """Regression: on Windows, os.kill(pid,0) misreported live workers as dead."""
    from backend.paper.worker_manager import worker_status, _pid_alive
    db = _db()
    from backend.strategy.trading_engine import BotState
    old = BotState._db
    BotState._db = db
    try:
        db.save_setting("paper_worker_pid", str(os.getpid()))
        db.save_setting("paper_worker_heartbeat", datetime.now(timezone.utc).isoformat())
        db.save_setting("paper_worker_status", "running")
        # worker_status reads DATABASE_PATH from the environment — point it at
        # this test's temp DB for the duration of the check.
        with mock.patch.dict(os.environ, {"DATABASE_PATH": db.db_path}):
            st = worker_status()
        assert st["worker_alive"] is True, st
        assert _pid_alive(os.getpid()) is True
    finally:
        BotState._db = old
        db.close()


# ─── 7. EOD / session determinism ────────────────────────────────────────────

def test_worker_started_after_eod_refuses_new_entries():
    """Startup after the square-off cutoff must not create trades."""
    db = _db()
    rt = next(_make_runtime(db))
    late = rt.now_fn().replace(hour=15, minute=30, second=0, microsecond=0)
    with mock.patch.object(rt, "now_fn", return_value=late):
        res = rt.submit_entry(dict(_ENTRY_SIGNAL))  # deliberately late
    assert getattr(res, "accepted", True) is False
    assert getattr(res, "reason", "") == "EOD_CUTOFF"
    assert db.get_open_positions() == []
    db.close()


def test_startup_before_market_open_does_not_trade_via_cutoff_rule():
    """Pre-open startup: entry attempts before EOD cutoff are allowed by the
    cutoff gate itself (market-hours gating lives in the scanner), but EOD
    square-off must still run deterministically when past cutoff."""
    assert is_past_square_off(datetime(2026, 9, 24, 15, 16), "15:15") is True
    assert is_past_square_off(datetime(2026, 9, 24, 15, 14), "15:15") is False


# ─── 8. Reconciliation fail-closed ───────────────────────────────────────────

def test_reconcile_orphan_broker_position_fails_closed():
    """A position known only to the in-memory broker (not in the durable
    ledger) cannot be self-healed by hydration — reconcile must fail closed
    and latch STOP_NEW_ENTRIES."""
    db = _db()
    rt = next(_make_runtime(db))
    rt.broker.restore_position(
        instrument_key="NSE_FO|88888", quantity=40, average_price=120.0,
        entry_time="", meta={"strategy": "V8_D_PULLBACK_ATM"},
    )
    rec = rt.reconcile()
    assert rec["ok"] is False
    assert rec["action"] == "STOP_NEW_ENTRIES"
    assert rt.kill.blocks_entries() is True
    db.close()


def test_reconcile_self_heals_ledger_orphan_by_hydration():
    """The production incident class (ledger open, broker empty after restart)
    must resolve by rehydration, not by halting trading forever."""
    db = _db()
    rt = next(_make_runtime(db))
    db.upsert_position(Position(
        symbol="NSE_FO|77777", quantity=25, average_price=200.0,
        entry_time=datetime.now(timezone.utc), instrument_key="NSE_FO|77777",
        extra={"stop_loss": 180.0, "target": 240.0, "lot_size": 25, "trade_id": "t-1"},
    ))
    rec = rt.reconcile()
    assert rec["ok"] is True, rec
    assert rt.broker.positions["NSE_FO|77777"]["stop_loss"] == 180.0
    db.close()


def test_eod_square_off_closes_restored_position_at_valid_mark():
    db = _db()
    rt = next(_make_runtime(db))
    assert _submit(rt).accepted
    db.close()

    db2 = DatabaseManager(db_path=db.db_path)
    rt2 = next(_make_runtime(db2))
    past = rt2.now_fn().replace(hour=15, minute=20, second=0, microsecond=0)
    out = rt2.run_eod(now=past, marks={"NSE_FO|69780": 265.0})
    assert out["ran"] is True
    assert len(out["closed"]) == 1
    assert out["closed"][0]["exit_price"] == 265.0
    assert db2.get_open_positions() == []
    db2.close()
