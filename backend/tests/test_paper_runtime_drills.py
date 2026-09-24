"""Paper-mode runtime drills. Does not change V8-D strategy logic."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

from backend.paper.paper_runtime import PaperStartupError, PaperTradingRuntime, require_paper_env
from backend.execution.kill_switch import FULL_SYSTEM_STOP, STOP_NEW_ENTRIES


def _env(**extra):
    base = {
        "TRADING_MODE": "paper",
        "TRADING_STRATEGY": "V8_D_PULLBACK_ATM",
        "UPSTOX_ORDER_PRODUCT": "I",
        "TRADING_CAPITAL": "100000",
        "RISK_PER_TRADE_PCT": "0.025",
        "MAX_ALLOCATION_PCT": "0.18",
        "MAX_DAILY_LOSS_PCT": "0.02",
        "MAX_TRADES_PER_DAY": "3",
        "MAX_CONCURRENT_POSITIONS": "1",
        "EOD_SQUARE_OFF": "15:15",
    }
    base.update(extra)
    return base


def _runtime():
    path = os.path.join(tempfile.mkdtemp(), "paper.db")
    env = _env(DATABASE_PATH=path)
    morning = datetime(2026, 9, 20, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    with mock.patch.dict(os.environ, env, clear=False):
        rt = PaperTradingRuntime()
        rt.now_fn = lambda: morning
    return rt


def _signal(**kw):
    sig = {
        "timestamp": "2026-09-20T10:00:00+05:30",
        "instrument_key": "NSE_FO|99999",
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
    sig.update(kw)
    return sig


def test_paper_requires_strategy():
    with mock.patch.dict(os.environ, _env(TRADING_STRATEGY=""), clear=False):
        os.environ["TRADING_STRATEGY"] = ""
        try:
            require_paper_env()
            raise AssertionError("should fail")
        except PaperStartupError:
            pass


def test_paper_requires_product():
    with mock.patch.dict(os.environ, _env(UPSTOX_ORDER_PRODUCT=""), clear=False):
        os.environ["UPSTOX_ORDER_PRODUCT"] = ""
        try:
            require_paper_env()
            raise AssertionError("should fail")
        except PaperStartupError:
            pass


def test_restart_recovery_once():
    rt = _runtime()
    res = rt.submit_entry(_signal())
    assert res.accepted, res.reason
    rec1 = rt.reconcile()
    assert rec1["ok"] is True
    # restart
    path = rt.db.db_path
    morning = datetime(2026, 9, 20, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    with mock.patch.dict(os.environ, _env(DATABASE_PATH=path), clear=False):
        rt2 = PaperTradingRuntime()
        rt2.now_fn = lambda: morning
        rt2.broker.positions = dict(rt.broker.positions)
        rec2 = rt2.reconcile()
    assert rec2["ok"] is True
    again = rt2.submit_entry(_signal())
    assert again.accepted is False
    # Position still open → max positions or duplicate signal both valid blocks
    assert again.reason in ("duplicate_signal", "MAX_POSITIONS") or "MAX_POSITIONS" in again.reason


def test_reconcile_api_error_stops_entries():
    rt = _runtime()
    rt.broker.fail_get_positions = True
    rec = rt.reconcile()
    assert rec["ok"] is False
    assert rec["action"] == "STOP_NEW_ENTRIES"


def test_partial_and_reject_fills():
    rt = _runtime()
    rt.broker.next_fill_mode = "half"
    res = rt.submit_entry(_signal(timestamp="2026-09-20T10:05:00+05:30"))
    assert res.accepted
    assert res.order.filled_quantity == 37
    assert res.order.status.value == "PARTIALLY_FILLED"
    rt.broker.next_fill_mode = "reject"
    # Close partial position first so risk gate allows a second instrument attempt
    if "NSE_FO|99999" in rt.broker.positions or any(rt.broker.positions):
        for ik in list(rt.broker.positions.keys()):
            rt.on_option_quote(ik, 50.0, timestamp="2026-09-20T10:09:00+05:30")
    res2 = rt.submit_entry(_signal(timestamp="2026-09-20T10:10:00+05:30", instrument_key="NSE_FO|888"))
    assert res2.accepted, res2.reason
    assert res2.order is not None
    assert res2.order.status.value == "REJECTED"
    assert res2.order.filled_quantity == 0


def test_unknown_does_not_create_position():
    rt = _runtime()
    rt.broker.next_fill_mode = "unknown"
    res = rt.submit_entry(_signal(timestamp="2026-09-20T10:20:00+05:30", instrument_key="NSE_FO|777"))
    assert res.order.status.value == "UNKNOWN"
    assert "NSE_FO|777" not in rt.broker.positions


def test_eod_flattens_and_blocks():
    rt = _runtime()
    rt.submit_entry(_signal(timestamp="2026-09-20T10:30:00+05:30", instrument_key="NSE_FO|666"))
    now = datetime(2026, 9, 20, 15, 16, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    out = rt.run_eod(now)
    assert out["ran"] is True
    assert out["remaining"] == 0
    rt.now_fn = lambda: now
    blocked = rt.submit_entry(_signal(timestamp="2026-09-20T15:20:00+05:30", instrument_key="NSE_FO|555"))
    assert blocked.accepted is False


def test_kill_switch_persists():
    rt = _runtime()
    rt.kill.set_level(FULL_SYSTEM_STOP, "drill")
    path = rt.db.db_path
    with mock.patch.dict(os.environ, _env(DATABASE_PATH=path), clear=False):
        rt2 = PaperTradingRuntime()
    assert rt2.kill.level() == FULL_SYSTEM_STOP
    blocked = rt2.submit_entry(_signal())
    assert blocked.accepted is False
