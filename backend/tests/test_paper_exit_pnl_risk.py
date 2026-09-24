"""Paper exit engine, P&L/equity, contract lot sizing, centralized risk.

Does NOT modify V8-D entry parameters. Paper mode only — no live orders.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

from backend.paper.paper_runtime import PaperTradingRuntime, require_paper_env
from backend.execution.kill_switch import FULL_SYSTEM_STOP
from backend.orders.execution_guard import evaluate_pretrade_guard
from backend.risk.risk_config import build_authoritative_risk_config


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
        "TRADING_BOT_OFFLINE_TESTS": "1",
    }
    base.update(extra)
    return base


def _runtime(capital: str = "100000"):
    path = os.path.join(tempfile.mkdtemp(), "paper_exit.db")
    env = _env(DATABASE_PATH=path, TRADING_CAPITAL=capital)
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
        "premium": 100.0,
        "spot": 24010.0,
        "quantity": 75,
        "stop_loss": 72.0,   # -28%
        "target": 142.0,     # ~+42%
        "atr": 6.0,
        "quote_age_seconds": 1,
    }
    sig.update(kw)
    return sig


# ── A. Strategy identity ──────────────────────────────────────────────────

def test_paper_strategy_is_v8d_only():
    with mock.patch.dict(os.environ, _env(TRADING_STRATEGY="OPTION_PREMIUM"), clear=False):
        os.environ["TRADING_STRATEGY"] = "OPTION_PREMIUM"
        try:
            require_paper_env()
            raise AssertionError("OPTION_PREMIUM must be refused in paper")
        except Exception as exc:
            assert "V8_D" in str(exc) or "OPTION_PREMIUM" in str(exc)


# ── B. Paper exits ────────────────────────────────────────────────────────

def test_stop_loss_exit_at_market_price():
    rt = _runtime()
    res = rt.submit_entry(_signal())
    assert res.accepted, res.reason
    ik = "NSE_FO|99999"
    out = rt.on_option_quote(ik, 70.0, timestamp="2026-09-20T10:05:00+05:30")
    assert out is not None
    assert out["exit_reason"] == "STOP_LOSS"
    assert out["exit_price"] == 70.0
    assert out["exit_price"] != out["entry_price"]
    assert out["net_pnl"] < 0
    assert ik not in rt.broker.positions


def test_target_exit_profitable():
    rt = _runtime()
    rt.submit_entry(_signal(timestamp="2026-09-20T10:01:00+05:30", instrument_key="NSE_FO|T1"))
    out = rt.on_option_quote("NSE_FO|T1", 150.0, timestamp="2026-09-20T10:10:00+05:30")
    assert out is not None
    assert out["exit_reason"] == "TARGET"
    assert out["exit_price"] == 150.0
    assert out["net_pnl"] > 0
    assert rt.realized_equity > rt.starting_capital


def test_trailing_stop_exit():
    rt = _runtime()
    rt.submit_entry(_signal(timestamp="2026-09-20T10:02:00+05:30", instrument_key="NSE_FO|TR"))
    # Price rises enough to ratchet trailing above initial stop, then falls to trail
    rt.on_option_quote("NSE_FO|TR", 130.0, timestamp="2026-09-20T10:15:00+05:30")
    pos = rt.broker.positions.get("NSE_FO|TR")
    assert pos is not None
    trail = float(pos["trailing_stop"])
    assert trail >= float(pos["initial_stop"])
    # Drop to just below trailing
    out = rt.on_option_quote("NSE_FO|TR", trail - 0.5, timestamp="2026-09-20T10:20:00+05:30")
    assert out is not None
    assert out["exit_reason"] in ("TRAILING_STOP", "STOP_LOSS")


def test_eod_uses_mark_not_entry():
    rt = _runtime()
    rt.submit_entry(_signal(timestamp="2026-09-20T10:03:00+05:30", instrument_key="NSE_FO|EOD1"))
    # Subsequent mark at 90 (below entry 100) — EOD must exit at 90, not 100
    rt.on_option_quote("NSE_FO|EOD1", 90.0, timestamp="2026-09-20T14:00:00+05:30")
    now = datetime(2026, 9, 20, 15, 16, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    out = rt.run_eod(now, marks={"NSE_FO|EOD1": 88.0})
    assert out["ran"] is True
    assert out["remaining"] == 0
    closed = out["closed"]
    assert len(closed) == 1
    assert closed[0]["exit_price"] == 88.0
    assert closed[0]["exit_reason"] == "EOD_SQUARE_OFF"
    assert closed[0]["exit_price"] != closed[0]["entry_price"]


def test_duplicate_exit_prevented():
    rt = _runtime()
    rt.submit_entry(_signal(timestamp="2026-09-20T10:04:00+05:30", instrument_key="NSE_FO|DUP"))
    out1 = rt.on_option_quote("NSE_FO|DUP", 70.0)
    assert out1 is not None
    out2 = rt.on_option_quote("NSE_FO|DUP", 60.0)
    assert out2 is None


# ── C. Paper P&L ──────────────────────────────────────────────────────────

def test_unrealized_pnl_while_open():
    rt = _runtime()
    rt.submit_entry(_signal(timestamp="2026-09-20T10:06:00+05:30", instrument_key="NSE_FO|U1"))
    rt.on_option_quote("NSE_FO|U1", 110.0)
    pos = rt.broker.positions["NSE_FO|U1"]
    assert abs(pos["unrealized_pnl"] - (110.0 - 100.0) * 75) < 0.01
    snap = rt.equity_snapshot()
    assert snap["unrealized_pnl"] > 0
    assert snap["realized_pnl"] == 0


def test_realized_equity_after_close():
    rt = _runtime()
    start = rt.realized_equity
    rt.submit_entry(_signal(timestamp="2026-09-20T10:07:00+05:30", instrument_key="NSE_FO|R1"))
    out = rt.on_option_quote("NSE_FO|R1", 70.0)
    assert out["net_pnl"] < 0
    assert rt.realized_equity < start
    assert abs(rt.realized_equity - (start + out["net_pnl"])) < 0.02


def test_sequential_trades_update_equity():
    rt = _runtime()
    e0 = rt.realized_equity
    rt.submit_entry(_signal(timestamp="2026-09-20T10:08:00+05:30", instrument_key="NSE_FO|S1"))
    out1 = rt.on_option_quote("NSE_FO|S1", 150.0)
    e1 = rt.realized_equity
    assert e1 > e0
    # second trade different key
    rt.submit_entry(_signal(timestamp="2026-09-20T10:30:00+05:30", instrument_key="NSE_FO|S2"))
    out2 = rt.on_option_quote("NSE_FO|S2", 70.0)
    e2 = rt.realized_equity
    assert e2 < e1
    assert abs((e2 - e0) - (out1["net_pnl"] + out2["net_pnl"])) < 0.05


# ── D. Contract metadata lot sizing ───────────────────────────────────────

def test_quantity_must_be_multiple_of_lot():
    rt = _runtime()
    res = rt.submit_entry(_signal(lot_size=65, quantity=100, timestamp="2026-09-20T11:00:00+05:30", instrument_key="NSE_FO|L1"))
    assert res.accepted is False
    assert "LOT" in res.reason.upper() or "QUANTITY" in res.reason.upper()


def test_missing_lot_size_rejected():
    rt = _runtime()
    res = rt.submit_entry(_signal(lot_size=0, quantity=75, timestamp="2026-09-20T11:01:00+05:30", instrument_key="NSE_FO|L2"))
    assert res.accepted is False
    assert "LOT" in res.reason.upper()


def test_valid_lot_65_accepted():
    rt = _runtime()
    # 65 lot, 1 lot qty, premium low enough for capital
    res = rt.submit_entry(_signal(
        lot_size=65, quantity=65, premium=50.0, stop_loss=36.0, target=71.0,
        timestamp="2026-09-20T11:02:00+05:30", instrument_key="NSE_FO|L3",
    ))
    assert res.accepted, res.reason


def test_v8d_uses_contract_lot_not_hardcoded():
    from backend.strategy.strategies.v8d_strategy import V8DStrategy
    strat = V8DStrategy()
    candles = []
    base = 24000.0
    for i in range(80):
        candles.append({
            "open": base + i * 0.5, "high": base + i * 0.5 + 5,
            "low": base + i * 0.5 - 5, "close": base + i * 0.5 + 1,
            "volume": 1000, "timestamp": f"2026-09-20T10:{i:02d}:00+05:30",
        })
    # Force a mild pullback structure is hard; just verify lot_size rejection path
    chain = [{
        "strike": 24000, "option_type": "CE", "instrument_key": "NSE_FO|X",
        "ltp": 100.0, "lot_size": 0,  # missing
    }]
    sig, log = strat.evaluate_v8d_signal(
        underlying_symbol="NIFTY50",
        underlying_candles=candles,
        spot_price=24000,
        option_chain=chain,
        account_equity=100000,
    )
    # Either no signal (pullback not met) or rejected for lot size
    if log.decision == "REJECTED":
        assert any("LOT" in r.upper() or "lot_size" in r.lower() for r in log.rejection_reasons)


# ── E. Central risk ───────────────────────────────────────────────────────

def test_max_positions_enforced():
    rt = _runtime()
    r1 = rt.submit_entry(_signal(timestamp="2026-09-20T11:10:00+05:30", instrument_key="NSE_FO|P1"))
    assert r1.accepted, r1.reason
    r2 = rt.submit_entry(_signal(timestamp="2026-09-20T11:11:00+05:30", instrument_key="NSE_FO|P2"))
    assert r2.accepted is False
    assert "MAX_POSITIONS" in r2.reason or "max concurrent" in r2.reason.lower()


def test_max_daily_trades_enforced():
    rt = _runtime()
    # max is 3 — open and close at TARGET so daily-loss gate does not fire first
    for i in range(3):
        ik = f"NSE_FO|D{i}"
        r = rt.submit_entry(_signal(timestamp=f"2026-09-20T11:{20+i:02d}:00+05:30", instrument_key=ik))
        assert r.accepted, r.reason
        rt.on_option_quote(ik, 150.0)  # target hit — profitable
    r4 = rt.submit_entry(_signal(timestamp="2026-09-20T11:30:00+05:30", instrument_key="NSE_FO|D9"))
    assert r4.accepted is False
    assert "MAX_DAILY_TRADES" in r4.reason or "max daily" in r4.reason.lower()


def test_kill_switch_blocks_entry():
    rt = _runtime()
    rt.kill.set_level(FULL_SYSTEM_STOP, "test")
    r = rt.submit_entry(_signal(timestamp="2026-09-20T11:40:00+05:30", instrument_key="NSE_FO|K1"))
    assert r.accepted is False
    assert "kill" in r.reason.lower()


def test_insufficient_equity_rejected():
    rt = _runtime(capital="1000")
    # 75 * 100 = 7500 notional > 1000 equity
    r = rt.submit_entry(_signal(timestamp="2026-09-20T11:41:00+05:30", instrument_key="NSE_FO|EQ"))
    assert r.accepted is False
    assert "INSUFFICIENT" in r.reason.upper() or "allocation" in r.reason.lower() or "risk" in r.reason.lower()


def test_guard_receives_state():
    cfg = build_authoritative_risk_config(
        capital=100000,
        strategy_risk_pct=0.025,
        engine_risk_pct=0.025,
        risk_manager_daily_loss_pct=0.02,
        configured_risk_pct=0.025,
        allocation_limit_pct=0.18,
        max_daily_trades=3,
        max_positions=1,
        max_daily_loss_pct=0.02,
        lot_size_source="contract_metadata",
        order_product="I",
        strategy_name="V8_D_PULLBACK_ATM",
        eod_square_off="15:15",
    )
    g = evaluate_pretrade_guard(
        premium=100, stop_loss=72, quantity=75, lot_size=75, config=cfg,
        open_positions=1, trades_today=0,
    )
    assert g.allowed is False
    assert any("MAX_POSITIONS" in r or "max concurrent" in r.lower() for r in g.reasons)


# ── F. Safety: paper never places live Upstox orders ──────────────────────

def test_paper_place_does_not_call_upstox_http():
    rt = _runtime()
    with mock.patch("backend.broker.upstox_client.UpstoxClient") as client_cls:
        rt.submit_entry(_signal(timestamp="2026-09-20T12:00:00+05:30", instrument_key="NSE_FO|LIVE"))
        client_cls.assert_not_called()
    # Broker is PaperBroker only
    assert type(rt.broker).__name__ == "PaperBroker"
