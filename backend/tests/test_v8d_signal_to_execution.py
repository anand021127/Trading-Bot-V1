"""Valid V8-D setup -> BUY -> AI -> risk -> paper execution, and
invalid setup -> NO_SIGNAL -> NOTHING downstream is called.

Real V8DStrategy on REAL 5-minute index candles (repo `real_data/`). The scanner
clock is pinned to just after the evaluated bar closes, so prices are never
altered. Only the option chain is a test input (the repo carries no option data).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from backend.paper import scan_state as ss
from backend.paper.market_scan_loop import PaperMarketScanner
from backend.strategy.strategies.v8d_strategy import V8DStrategy
from backend.strategy.v8d_diagnostics import explain_pullback
from backend.tests.test_ai_decision_layer import APPROVE_JSON
from backend.tests.test_ai_pipeline_e2e import _engine, _pipeline
from backend.tests.test_scanner_runtime_pipeline import FakeData, _fresh_hb, _scan, worker  # noqa: F401
from backend.tests.test_v8d_diagnostics import SYMBOLS, WINDOW, _chain_for, _load

ROOT = Path(__file__).resolve().parents[2]


class RealScanner(PaperMarketScanner):
    """Real scanner; the clock is pinned (instance attribute) to just after a real bar closed."""
    fixed_now: datetime

    def scan_once(self, runtime, **kw):
        kw.setdefault("now", self.fixed_now)
        return super().scan_once(runtime, **kw)


def _now_after_close(window, seconds_after=3):
    start = datetime.fromisoformat(window[-1]["timestamp"]).astimezone(timezone.utc)
    return start + timedelta(seconds=300 + seconds_after)


def _in_entry_window(w):
    t = w[-1]["timestamp"][11:16]
    return "09:35" <= t <= "14:30"


def _find(sym, pred):
    strat = V8DStrategy()
    c = _load(sym)
    for i in range(WINDOW, len(c)):
        w = c[i - WINDOW + 1: i + 1]
        if _in_entry_window(w) and datetime.fromisoformat(w[-1]["timestamp"]).weekday() < 5:
            e = explain_pullback(w, strat)
            if pred(e):
                return w, e
    raise AssertionError(f"no matching real window for {sym}")


def _scanner(worker, sym, w, engine, **kw):
    strat = worker.runtime.strategy
    spot = float(w[-1]["close"])
    sc = RealScanner(
        data=FakeData(candles=w, chain=_chain_for(strat, spot, sym), spot=spot),
        strategy=strat, underlying=sym, min_bars=60, max_candle_age_seconds=900,
        ai_engine=engine, ai_decision_pipeline_strategy="V8_D_PULLBACK_ATM", **kw)
    sc.fixed_now = _now_after_close(w)
    return sc


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PAPER_SYMBOL_SCAN_INTERVAL_SEC", "0")


# ── 1. valid setup → BUY → AI → risk → execution ────────────────────────────
@pytest.mark.parametrize("sym", SYMBOLS)
def test_valid_real_setup_reaches_ai_risk_and_paper_execution(worker, sym):
    w, e = _find(sym, lambda x: x["decision"] == "CE")
    worker.runtime.now_fn = lambda: _now_after_close(w).astimezone(timezone(timedelta(hours=5, minutes=30)))
    worker.db.save_setting("paper_reconcile_ok", "1")
    eng = _engine(worker, [APPROVE_JSON])
    rec = _scan(worker, _scanner(worker, sym, w, eng))
    assert rec["v8d"]["decision"] == "CE" and rec["v8d"]["consistent"] is True
    assert rec["v8d"]["strategy_decision"] == "ACCEPTED" or rec["traded"] is True, rec["reason"]
    assert rec["signal"] == "BUY" and rec["traded"] is True, (rec["reason"], rec.get("details", {}).get("rejection"))
    assert rec["outcome"] == "FILLED" and rec["ai"]["status"] == "APPROVED"
    assert len(eng.provider.calls) == 1                         # AI consulted exactly once, only because V8-D said BUY
    assert len(worker.db.list_trades()) == 1 and len(worker.db.get_open_positions()) == 1
    pipe, _ = _pipeline(worker)
    assert pipe["latest_signal"] == "BUY CE" and pipe["ai_decision"] == "APPROVED"
    assert pipe["risk_check"] == "PASS" and pipe["execution"] == "FILLED (PAPER)" and pipe["final"] == "FILLED (PAPER)"
    assert pipe["outcome"] == "FILLED" and pipe["primary_symbol"] == sym


# ── 2. invalid setup → NO_SIGNAL → nothing downstream is touched ────────────
@pytest.mark.parametrize("sym", SYMBOLS)
def test_no_signal_never_reaches_ai_risk_or_execution(worker, sym):
    w, e = _find(sym, lambda x: x["decision"] is None and x["evaluated"])
    worker.runtime.now_fn = lambda: _now_after_close(w).astimezone(timezone(timedelta(hours=5, minutes=30)))
    worker.db.save_setting("paper_reconcile_ok", "1")
    eng = _engine(worker, [APPROVE_JSON])
    with mock.patch.object(worker.runtime, "submit_entry", side_effect=AssertionError("must not be called")) as spy:
        rec = _scan(worker, _scanner(worker, sym, w, eng))
    assert spy.call_count == 0                                   # execution never attempted
    assert eng.provider.calls == []                              # AI never consulted
    assert rec["outcome"] == "NO_SIGNAL" and rec["reason"] == "no_trade:NO_SIGNAL"
    assert rec["traded"] is False and rec["risk_decision"] == "NOT_EVALUATED"
    assert rec["execution_decision"] == "NOT_ATTEMPTED" and rec["ai"]["status"] == "NOT_EVALUATED"
    assert not worker.db.list_trades()
    # the scan explains exactly which conditions failed, with the real numbers
    v = rec["v8d"]
    assert v["consistent"] is True and v["decision"] is None and v["failed"] and v["binding_condition"]
    for k in ("ema20", "ema50", "ema_separation_pct", "rsi", "atr14_underlying", "price", "pullback_band"):
        assert v.get(k) is not None, k
    assert v["ema20"] == pytest.approx(e["ema20"]) and v["rsi"] == pytest.approx(e["rsi"])
    assert rec["chain"]["option_chain_count"] == 2 and rec["chain"]["atm_strike"]
    pipe, _ = _pipeline(worker)
    assert pipe["latest_signal"] == "NO SIGNAL" and pipe["outcome"] == "NO_SIGNAL"
    assert pipe["ai_decision"] == "NOT EVALUATED" and pipe["risk_check"] == "NOT EVALUATED"
    assert pipe["execution"] == "NOT ATTEMPTED" and pipe["final"] == "NO TRADE"
    assert "REJECT" not in pipe["latest_signal"].upper()          # NO_SIGNAL is never called a rejected signal
    assert pipe["signal_detail"] == e["reason"]


# ── 3. completed-candle feed (backtest parity) ──────────────────────────────
def test_forming_candle_is_excluded_from_evaluation(worker):
    strat = V8DStrategy()
    # a real window whose LAST bar completes a setup, but whose previous bars alone do not
    sym = "NIFTY50"
    c = _load(sym)
    pick = None
    for i in range(WINDOW, len(c)):
        w = c[i - WINDOW + 1: i + 1]
        if _in_entry_window(w) and explain_pullback(w, strat)["decision"] and \
                explain_pullback(w[:-1], strat)["decision"] is None:
            pick = w
            break
    assert pick is not None
    start = datetime.fromisoformat(pick[-1]["timestamp"]).astimezone(timezone.utc)
    worker.db.save_setting("paper_reconcile_ok", "1")
    eng = _engine(worker, [APPROVE_JSON])
    sc = _scanner(worker, sym, pick, eng)
    sc.fixed_now = start + timedelta(seconds=100)                 # bar still forming (100s of 300s)
    worker.runtime.now_fn = lambda: sc.fixed_now.astimezone(timezone(timedelta(hours=5, minutes=30)))
    rec = _scan(worker, sc)
    assert rec["details"]["forming_candle_excluded"] is True and rec["details"]["last_candle_complete"] is False
    assert rec["details"]["candles_evaluated"] == len(pick) - 1
    assert rec["traded"] is False and rec["outcome"] == "NO_SIGNAL"      # no BUY on a half-built bar
    assert eng.provider.calls == []
    # once the bar closes, the very same data produces the BUY
    sc2 = _scanner(worker, sym, pick, eng)
    rec2 = _scan(worker, sc2)
    assert rec2["details"]["forming_candle_excluded"] is False and rec2["details"]["last_candle_complete"] is True
    assert rec2["signal"] == "BUY"


def test_forming_candle_opt_out_restores_the_old_feed(worker, monkeypatch):
    monkeypatch.setenv("PAPER_EVAL_FORMING_CANDLE", "1")
    w, _ = _find("NIFTY50", lambda x: x["decision"] is None)
    sc = _scanner(worker, "NIFTY50", w, _engine(worker, [APPROVE_JSON]))
    sc.fixed_now = datetime.fromisoformat(w[-1]["timestamp"]).astimezone(timezone.utc) + timedelta(seconds=100)
    rec = _scan(worker, sc)
    assert rec["details"]["forming_candle_excluded"] is False and rec["details"]["candles_evaluated"] == len(w)


# ── 4. all configured symbols are scanned, independently ────────────────────
def test_all_six_symbols_are_scanned_and_one_failure_does_not_stop_the_others(worker):
    worker.db.save_setting("paper_reconcile_ok", "1")
    eng = _engine(worker, [APPROVE_JSON])
    worker.scanners = {}
    for sym in SYMBOLS:
        w, _ = _find(sym, lambda x: x["decision"] is None and x["evaluated"])
        sc = _scanner(worker, sym, w, eng)
        if sym == "SENSEX":
            sc.data = FakeData(candle_exc=RuntimeError("401 Bearer abcdef0123456789abcdef"))
        worker.scanners[sym] = sc
    worker.scanner = worker.scanners["NIFTY50"]
    worker._next_scan_mono = 0.0
    worker._maybe_scan()
    rows = {r["symbol"]: r for r in ss.read_symbol_records(worker.db)}
    assert set(rows) == set(SYMBOLS)
    assert rows["SENSEX"]["outcome"] == "DATA_ERROR" and "abcdef0123456789abcdef" not in json.dumps(rows["SENSEX"])
    for sym in SYMBOLS:
        if sym != "SENSEX":
            assert rows[sym]["outcome"] == "NO_SIGNAL" and rows[sym]["binding"], sym
            assert rows[sym]["ema20"] is not None and rows[sym]["rsi"] is not None
    assert eng.provider.calls == [] and not worker.db.list_trades()
    pipe, st = _pipeline(worker)
    assert pipe["outcome"] == "NO_SIGNAL"                         # NO_SIGNAL outranks a single symbol's data error
    assert {r["symbol"] for r in pipe["symbols"]} == set(SYMBOLS)
    assert {r["symbol"]: r["outcome"] for r in pipe["symbols"]}["SENSEX"] == "DATA_ERROR"
    assert st["state"] in (ss.RUNNING_NO_SIGNAL, ss.RUNNING_DATA_ERROR)


def test_a_fill_on_one_symbol_is_the_primary_record_and_counts_for_every_symbol(worker):
    worker.db.save_setting("paper_reconcile_ok", "1")
    eng = _engine(worker, [APPROVE_JSON])
    wb, _ = _find("BANKNIFTY", lambda x: x["decision"] == "CE")
    wn, _ = _find("NIFTY50", lambda x: x["decision"] is None and x["evaluated"])
    worker.runtime.now_fn = lambda: _now_after_close(wb).astimezone(timezone(timedelta(hours=5, minutes=30)))
    worker.scanners = {"NIFTY50": _scanner(worker, "NIFTY50", wn, eng), "BANKNIFTY": _scanner(worker, "BANKNIFTY", wb, eng)}
    worker.scanner = worker.scanners["NIFTY50"]
    worker._next_scan_mono = 0.0
    worker._maybe_scan()
    assert len(worker.db.list_trades()) == 1 and worker.runtime.trades_today == 1
    pipe, _ = _pipeline(worker)
    assert pipe["outcome"] == "FILLED" and pipe["primary_symbol"] == "BANKNIFTY"
    assert {r["symbol"]: r["outcome"] for r in pipe["symbols"]} == {"NIFTY50": "NO_SIGNAL", "BANKNIFTY": "FILLED"}


def test_per_symbol_cadence_is_respected(worker, monkeypatch):
    monkeypatch.setenv("PAPER_SYMBOL_SCAN_INTERVAL_SEC", "3600")
    worker.db.save_setting("paper_reconcile_ok", "1")
    w, _ = _find("NIFTY50", lambda x: x["decision"] is None and x["evaluated"])
    w2, _ = _find("BANKNIFTY", lambda x: x["decision"] is None and x["evaluated"])
    eng = _engine(worker, [APPROVE_JSON])
    worker.scanners = {"NIFTY50": _scanner(worker, "NIFTY50", w, eng), "BANKNIFTY": _scanner(worker, "BANKNIFTY", w2, eng)}
    worker.scanner = worker.scanners["NIFTY50"]
    for _ in range(3):
        worker._next_scan_mono = 0.0
        worker._maybe_scan()
    assert int(worker.db.get_setting(ss.SCAN_SEQ_KEY)) == 2          # one scan per symbol, not three


def test_default_universe_is_all_six_and_is_overridable():
    from backend.api.routers.backtest import VALID_OPTION_INDICES
    assert ss.parse_underlyings({}) == list(ss.PAPER_DEFAULT_UNDERLYINGS) == list(VALID_OPTION_INDICES) == SYMBOLS
    assert ss.parse_underlyings({"PAPER_UNDERLYINGS": "sensex, nifty50, bogus"}) == ["SENSEX", "NIFTY50"]
    assert ss.parse_underlyings({"PAPER_UNDERLYING": "NIFTY50"}) == ["NIFTY50"]       # legacy single-symbol opt-in
    assert ss.parse_underlyings({"PAPER_UNDERLYINGS": "bogus"}) == SYMBOLS            # never invents symbols
