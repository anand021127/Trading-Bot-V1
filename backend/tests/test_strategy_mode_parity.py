"""Backtest / Paper / Live strategy parity — V8_D_PULLBACK_ATM.

Genuine bug fixed (proven in this file): the backtest evaluated V8-D's
``max_daily_trades`` gate against a SNAPSHOT of ``trades_opened_today`` taken
before any position on the bar opened, so N symbols signalling on ONE bar all
passed and the backtest opened N trades (6 with a cap of 3). Paper and live
fill sequentially (each fill bumps ``trades_today`` before the next
evaluation), so they never could. The backtest now re-asks the STRATEGY with
the up-to-date count — the cap lives only in the strategy.

Nothing here changes V8-D parameters or entry logic.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.backtest.engine import BacktestEngine
from backend.strategy.signal import SignalType, StrategySignal
from backend.strategy.strategies.v8d_strategy import V8DStrategy
from backend.tests.test_multi_symbol_backtest import generate_candles

ROOT = Path(__file__).resolve().parents[2]
SYMS = ["NIFTY50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"]
CAP = V8DStrategy().max_daily_trades      # the strategy is the single source of the cap


def _gate_eval(seen):
    """Stand-in for MultiStrategyEngine.evaluate that applies EXACTLY the
    V8-D daily-cap rule (``trades_today >= max_daily_trades`` -> reject with
    the strategy's own message). Records the trades_today each call saw."""
    def ev(symbol, window, context=None, strategy_names=None):
        tt = int((context or {}).get("trades_today") or 0)
        if len(window) != 21:
            return [StrategySignal(strategy_name="V8_D_PULLBACK_ATM", symbol=symbol, signal=SignalType.NONE)]
        seen.append((symbol, tt))
        if tt >= CAP:
            s = StrategySignal(strategy_name="V8_D_PULLBACK_ATM", symbol=symbol, signal=SignalType.NONE)
            s.rejected_reasons = [f"Daily trade limit reached: {tt}/{CAP}"]
            return [s]
        c = window[-1]["close"]
        return [StrategySignal(strategy_name="V8_D_PULLBACK_ATM", symbol=symbol, signal=SignalType.BUY,
                               entry_price=c, stop_loss=c - 50, target=c + 100, confidence=80.0)]
    return ev


def _run(symbols, **engine_kw):
    seen = []
    data = {s: generate_candles(s, count=40, start_price=20000.0) for s in symbols}
    e = BacktestEngine(min_candles_required=20, max_simultaneous_positions=len(symbols), **engine_kw)
    e.strategy_engine.evaluate = MagicMock(side_effect=_gate_eval(seen))
    return e.run(data, strategy_names=["V8_D_PULLBACK_ATM"]), seen


# ── the bug ─────────────────────────────────────────────────────────────────
def test_backtest_same_bar_signals_cannot_exceed_daily_cap():
    res, seen = _run(SYMS)
    assert res.trades_taken == CAP, f"{res.trades_taken} trades opened, cap is {CAP}"
    assert len(res.trade_log) == CAP
    # every symbol below the cap was re-asked with an UPDATED count
    counts = [tt for _, tt in seen]
    assert max(counts) == CAP, "strategy was never re-evaluated with the running count"


def test_same_bar_cap_rejections_are_the_strategys_own_and_are_counted():
    res, _ = _run(SYMS)
    counts = res.rejection_reason_counts
    key = f"Daily trade limit reached: {CAP}/{CAP}"
    # 6 symbols - 3 opened = 3 rejected by the STRATEGY's own rule, verbatim
    assert counts.get(key) == len(SYMS) - CAP, counts
    assert res.rejected_signals_total_count >= len(SYMS) - CAP


def test_cap_only_binds_when_reached_and_is_deterministic():
    few, _ = _run(SYMS[:2])
    assert few.trades_taken == 2                       # under the cap: untouched
    a, _ = _run(SYMS)
    b, _ = _run(list(reversed(SYMS)))
    assert sorted(t["underlying"] for t in a.trade_log) == sorted(t["underlying"] for t in b.trade_log)
    assert a.net_profit == b.net_profit


def test_cap_resets_each_new_calendar_day():
    seen = []
    day1 = {s: generate_candles(s, count=40, start_price=20000.0, start_time="2024-01-02T09:15:00") for s in SYMS[:4]}
    day2 = {s: generate_candles(s, count=40, start_price=20000.0, start_time="2024-01-03T09:15:00") for s in SYMS[:4]}
    merged = {s: day1[s] + day2[s] for s in day1}
    e = BacktestEngine(min_candles_required=20, max_simultaneous_positions=4)

    def ev(symbol, window, context=None, strategy_names=None):
        # fire on the 21st bar of EACH day: window length 21 (day 1), 61 (day 2)
        tt = int((context or {}).get("trades_today") or 0)
        if len(window) in (21, 61):
            if tt >= CAP:
                s = StrategySignal(strategy_name="V8_D_PULLBACK_ATM", symbol=symbol, signal=SignalType.NONE)
                s.rejected_reasons = [f"Daily trade limit reached: {tt}/{CAP}"]
                return [s]
            c = window[-1]["close"]
            return [StrategySignal(strategy_name="V8_D_PULLBACK_ATM", symbol=symbol, signal=SignalType.BUY,
                                   entry_price=c, stop_loss=c - 50, target=c + 100, confidence=80.0)]
        return [StrategySignal(strategy_name="V8_D_PULLBACK_ATM", symbol=symbol, signal=SignalType.NONE)]

    e.strategy_engine.evaluate = MagicMock(side_effect=ev)
    res = e.run(merged, strategy_names=["V8_D_PULLBACK_ATM"])
    by_day = {}
    for t in res.trade_log:
        by_day.setdefault(str(t["entry_time"])[:10], 0)
        by_day[str(t["entry_time"])[:10]] += 1
    assert by_day and all(n <= CAP for n in by_day.values()), by_day


# ── shared strategy identity across the three modes ─────────────────────────
def test_all_three_modes_build_identical_v8d_parameters():
    from backend.config.strategy_registry import load_strategy
    from backend.copilot.strategy_context import V8D_PARAMS
    bt = load_strategy("V8_D_PULLBACK_ATM")            # backtest API + PaperTradingRuntime path
    live = V8DStrategy()                               # TradingEngine.evaluate_configured_strategy path
    keys = ("stop_loss_pct", "target_pct", "max_account_risk_pct", "max_capital_alloc_pct",
            "max_daily_trades", "ema_fast", "ema_slow", "rsi_period", "use_atr_stop", "atr_stop_mult")
    for k in keys:
        assert getattr(bt, k) == getattr(live, k) == V8D_PARAMS[k], k
    assert type(bt) is V8DStrategy and bt.name == "V8_D_PULLBACK_ATM"
    # frozen spec values (guard against silent tuning)
    assert (bt.stop_loss_pct, bt.target_pct, bt.max_account_risk_pct, bt.max_capital_alloc_pct,
            bt.max_daily_trades) == (0.28, 0.42, 0.025, 0.18, 3)


def test_paper_runtime_uses_the_registry_strategy():
    src = (ROOT / "backend/paper/paper_runtime.py").read_text(encoding="utf-8")
    assert "self.strategy = load_strategy(" in src
    bt = (ROOT / "backend/api/routers/backtest.py").read_text(encoding="utf-8")
    assert "load_strategies(strategies)" in bt


def test_live_engine_feeds_running_trades_today_to_the_same_strategy():
    src = (ROOT / "backend/strategy/trading_engine.py").read_text(encoding="utf-8")
    assert "strat = V8DStrategy()" in src
    assert re.search(r"trades_today\s*=\s*int\(st\.get\(\"trades_today\"\)", src)
    assert re.search(r"trades_today\s*=\s*trades_today", src)


def test_paper_scan_enforces_the_same_strategy_cap_and_message():
    """Paper path with the REAL V8DStrategy: at the cap the strategy itself
    rejects with the identical message the backtest re-evaluation produces."""
    from backend.database.db_manager import DatabaseManager
    from backend.paper.paper_runtime import PaperTradingRuntime
    from backend.tests.test_scanner_runtime_pipeline import FakeData, FixedNowScanner, TRADING_NOW
    path = os.path.join(tempfile.mkdtemp(), "cap.db")
    os.environ.update({"DATABASE_PATH": path, "TRADING_MODE": "paper",
                       "TRADING_STRATEGY": "V8_D_PULLBACK_ATM", "UPSTOX_ORDER_PRODUCT": "I",
                       "TRADING_CAPITAL": "100000", "RISK_PER_TRADE_PCT": "0.025"})
    db = DatabaseManager(db_path=path)
    rt = PaperTradingRuntime(db=db)
    rt.now_fn = lambda: TRADING_NOW
    sc = FixedNowScanner(data=FakeData(), strategy=rt.strategy, min_bars=60)
    res = sc.scan_once(rt, trades_today=CAP)
    assert res.traded is False and res.signal != "BUY"
    rej = " ".join(str(x) for x in (res.details.get("rejection") or []))
    assert f"Daily trade limit reached: {CAP}/{CAP}" in rej, res.details
    db.close()


def test_backtest_fix_adds_no_order_placement_or_strategy_constant():
    src = (ROOT / "backend/backtest/engine.py").read_text(encoding="utf-8")
    seg = src[src.index("SAME-BAR DAILY-COUNT PARITY"):src.index("PHASE 10: optional AI decision-filter layer")]
    assert "max_daily_trades" not in seg.replace("V8-D max_daily_trades", "")  # cap not duplicated in engine
    assert "place_order" not in seg and "OrderManager" not in seg
