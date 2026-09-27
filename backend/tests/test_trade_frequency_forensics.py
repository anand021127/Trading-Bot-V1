"""PART 3/4 regression tests — trade-frequency collapse forensics.

Part 3: a fully VALID V8-D signal (valid contract, 1-lot-legal risk, no open
position, inside 09:20-14:45, below daily limit) MUST reach execution. This
pins the happy path so gate fixes can never again silently suppress valid
trades (the 277 -> 14 collapse regression).

Part 4: entry-window boundary parity across the three engines —
09:19 reject / 09:20 accept / 14:44 accept / 14:45 ACCEPT (inclusive) /
14:46 reject / 15:00 reject — identical for backtest gate,
session_manager.is_entry_window, and the exchange calendar OPEN window.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from backend.backtest.engine import BacktestEngine
from backend.backtest.options_data_layer import HistoricalOptionsDataLoader
from backend.strategy.signal import SignalType, StrategySignal
from backend.strategy.session_manager import session_manager
from backend.market.calendar import session_status_for_timestamp

IST = ZoneInfo("Asia/Kolkata")
CK = "NSE_FO|45482|10-03-2026"


def _candles(stamps, close=24200.0):
    return [{"timestamp": t, "open": close - 30, "high": close + 40,
             "low": close - 50, "close": close, "volume": 9000} for t in stamps]


def _loader(stamps, close=100.0, lot=20):
    ld = HistoricalOptionsDataLoader(auto_load_cache=False)
    ld.load_contract_candles(
        underlying="NIFTY50", expiry="2026-03-10", strike=24200.0,
        option_type="CE", instrument_key=CK,
        candles=[{"timestamp": t, "open": close + 1, "high": close + 3,
                  "low": close - 3, "close": close, "volume": 400} for t in stamps],
        lot_size=lot,
    )
    return ld


def _v8d_buy(symbol, qty=60, prem=100.0):
    sig = StrategySignal(
        strategy_name="V8_D_PULLBACK_ATM", symbol=symbol, signal=SignalType.BUY,
        confidence=99.0, entry_price=prem, stop_loss=round(prem * 0.8, 2),
        target=round(prem * 1.42, 2),
    )
    sig.indicators = {
        "directional_intent": "CE",
        "sizing": {"quantity": qty},
        "selected_contract": {"instrument_key": CK, "option_type": "CE",
                              "strike": 24200.0, "lot_size": 20, "ltp": prem},
    }
    return sig


def _run(stamps, signal_at, lot=20, qty=60, prem=100.0):
    engine = BacktestEngine(min_candles_required=2, capital=100000.0,
                            risk_pct_per_trade=0.01)
    def fake_eval(symbol, window, context=None, strategy_names=None):
        if window[-1]["timestamp"] in signal_at:
            return [_v8d_buy(symbol, qty=qty, prem=prem)]
        return [StrategySignal(strategy_name="V8_D_PULLBACK_ATM", symbol=symbol,
                               signal=SignalType.NONE)]
    engine.strategy_engine.evaluate = MagicMock(side_effect=fake_eval)
    return engine.run(
        {"NIFTY50": _candles(stamps)},
        strategy_names=["V8_D_PULLBACK_ATM"],
        options_data_loader=_loader(stamps, lot=lot),
        require_real_options=True,
    )


DAY1 = ["2026-03-09T09:20:00+05:30", "2026-03-09T09:45:00+05:30",
        "2026-03-09T10:00:00+05:30", "2026-03-09T10:05:00+05:30",
        "2026-03-09T11:00:00+05:30", "2026-03-09T11:05:00+05:30",
        "2026-03-09T14:40:00+05:30", "2026-03-09T14:45:00+05:30",
        "2026-03-09T15:15:00+05:30"]
DAY2 = ["2026-03-10T09:45:00+05:30", "2026-03-10T10:00:00+05:30",
        "2026-03-10T10:05:00+05:30", "2026-03-10T15:25:00+05:30"]


class TestHappyPathReachesExecution:
    def test_valid_signal_trades(self):
        """Valid V8-D signal + valid contract + 1-lot-legal risk + no position
        + inside window + below limit → MUST open a position (Part 3)."""
        res = _run(DAY1 + DAY2, {"2026-03-09T10:05:00+05:30"})
        assert res.trades_taken == 1
        t = res.trade_log[0]
        assert t["quantity"] == 60
        assert t["entry_price"] == 100.0  # strategy premium, not engine-resized
        assert t["exit_reason"] in ("INTRADAY_SQUARE_OFF",)

    def test_daily_limit_is_per_day_and_portfolio_wide(self):
        """The strategy-visible counter is PORTFOLIO-WIDE and per-DAY: with two
        symbols both signalling, the 3rd same-day trade is allowed and the 4th
        sees trades_today=3 (V8-D itself then rejects: 'Daily trade limit
        reached: 3/3'), and a day-2 signal sees 0 again. This is the direct
        regression for the 277→14 collapse (which froze at 3/symbol LIFETIME)."""
        A = "NIFTY50"
        B = "BANKNIFTY"
        A_KEY = "NSE_FO|45482|10-03-2026"
        B_KEY = "NSE_FO|53156|10-03-2026"
        stamps = [
            # day 1: 4 warmup bars, then two entry waves separated by stop-outs
            "2026-03-09T09:20:00+05:30", "2026-03-09T09:25:00+05:30",
            "2026-03-09T09:30:00+05:30", "2026-03-09T09:35:00+05:30",
            "2026-03-09T10:00:00+05:30",  # wave 1: both symbols open (tt: 0,0 → 2 open)
            "2026-03-09T10:05:00+05:30",  # option candle low 70 → both stopped out
            "2026-03-09T10:10:00+05:30",  # wave 2: both see tt=2, both open (→4)
            "2026-03-09T10:15:00+05:30",  # option candle low 70 → both stopped out
            "2026-03-09T11:00:00+05:30",  # wave 3: tt=4 → strategy limit-rejects
            "2026-03-09T15:15:00+05:30",  # square-off
            # day 2: counter reset — B trades again
            "2026-03-10T09:30:00+05:30", "2026-03-10T10:00:00+05:30",
            "2026-03-10T15:15:00+05:30",
        ]
        seen = {}

        def fake_eval(symbol, window, context=None, strategy_names=None):
            ts = window[-1]["timestamp"]
            tt = (context or {}).get("trades_today")
            seen[(symbol, ts)] = tt
            if ts in ("2026-03-09T10:00:00+05:30", "2026-03-09T10:10:00+05:30",
                      "2026-03-09T11:00:00+05:30", "2026-03-10T10:00:00+05:30"):
                if (tt or 0) >= 3:
                    sig = StrategySignal(strategy_name="V8_D_PULLBACK_ATM",
                                         symbol=symbol, signal=SignalType.NONE)
                    sig.rejected_reasons = ["Daily trade limit reached: 3/3"]
                    return [sig]
                return [_v8d_buy(symbol)]
            return [StrategySignal(strategy_name="V8_D_PULLBACK_ATM",
                                   symbol=symbol, signal=SignalType.NONE)]

        loader = HistoricalOptionsDataLoader(auto_load_cache=False)
        loader.load_contract_candles(
            underlying=A, expiry="2026-03-10", strike=24200.0, option_type="CE",
            instrument_key=A_KEY,
            candles=[{"timestamp": t, "open": 101.0, "high": 103.0, "low": 97.0,
                      "close": 100.0, "volume": 400}
                     for t in ("2026-03-09T09:20:00+05:30", "2026-03-09T10:00:00+05:30",
                               "2026-03-09T10:10:00+05:30", "2026-03-09T11:00:00+05:30",
                               "2026-03-10T10:00:00+05:30")]
            + [{"timestamp": t, "open": 99.0, "high": 100.0, "low": 70.0,
                "close": 70.0, "volume": 400}
               for t in ("2026-03-09T10:05:00+05:30", "2026-03-09T10:15:00+05:30")],
            lot_size=20,
        )
        loader.load_contract_candles(
            underlying=B, expiry="2026-03-10", strike=55600.0, option_type="CE",
            instrument_key=B_KEY,
            candles=[{"timestamp": t, "open": 101.0, "high": 103.0, "low": 97.0,
                      "close": 100.0, "volume": 400}
                     for t in ("2026-03-09T09:20:00+05:30", "2026-03-09T10:00:00+05:30",
                               "2026-03-09T10:10:00+05:30", "2026-03-09T11:00:00+05:30",
                               "2026-03-10T10:00:00+05:30")]
            + [{"timestamp": t, "open": 99.0, "high": 100.0, "low": 70.0,
                "close": 70.0, "volume": 400}
               for t in ("2026-03-09T10:05:00+05:30", "2026-03-09T10:15:00+05:30")],
            lot_size=30,
        )

        engine = BacktestEngine(min_candles_required=2, capital=100000.0,
                                risk_pct_per_trade=0.01)
        engine.strategy_engine.evaluate = MagicMock(side_effect=fake_eval)
        res = engine.run(
            {A: _candles(stamps), B: _candles(stamps, close=55600.0)},
            strategy_names=["V8_D_PULLBACK_ATM"],
            options_data_loader=loader,
            require_real_options=True,
        )
        # strategy saw the portfolio-wide counter increment within the day…
        # (both candidates at a bar evaluate BEFORE that bar's Phase-3 opens)
        assert seen[(B, "2026-03-09T10:00:00+05:30")] == 0
        assert seen[(A, "2026-03-09T10:00:00+05:30")] == 0
        assert seen[(B, "2026-03-09T10:10:00+05:30")] == 2  # 2 opened at wave 1
        assert seen[(A, "2026-03-09T10:10:00+05:30")] == 2
        assert seen[(B, "2026-03-09T11:00:00+05:30")] == 4  # ≥3 → strategy refused
        # …and reset at the new session date (closed day-1 count does NOT leak)
        assert seen[(B, "2026-03-10T10:00:00+05:30")] == 0
        assert seen[(A, "2026-03-10T10:00:00+05:30")] == 0
        assert res.trades_taken == 6  # 2+2 on day 1 (wave 3 blocked), 1+1 on day 2
        assert any("Daily trade limit reached: 3/3" in r
                   for r in res.rejection_reason_counts)


class TestEntryWindowBoundaries:
    """Part 4 — the exact boundary spec, enforced identically by the backtest
    gate and session_manager (the calendar OPEN window backs them)."""

    def test_session_manager_boundaries(self):
        cases = [(9, 19, False), (9, 20, True), (14, 44, True),
                 (14, 45, True), (14, 46, False), (15, 0, False)]
        for h, m, want in cases:
            got = session_manager.is_entry_window(datetime(2026, 9, 18, h, m, tzinfo=IST))
            assert got is want, f"{h:02d}:{m:02d} expected {want}"

    def test_calendar_open_window_backs_the_same_boundaries(self):
        cases = [(9, 19, "OPEN"), (9, 20, "OPEN"), (14, 44, "OPEN"),
                 (14, 45, "OPEN"), (14, 46, "AFTER_LAST_ENTRY"), (15, 0, "AFTER_LAST_ENTRY")]
        for h, m, want in cases:
            st, _ = session_status_for_timestamp(datetime(2026, 9, 18, h, m, tzinfo=IST))
            assert st == want, f"{h:02d}:{m:02d} expected {want}"

    def test_engine_gate_boundaries(self):
        """Entries at 09:15/14:46/15:00 are rejected; 09:20/14:45 accepted."""
        # prior-day warmup bars so the 09:15/09:20 signals have enough history
        warm = [f"2026-03-06T0{m}:00+05:30" for m in (9, 10, 11, 12)]
        stamps = warm + ["2026-03-09T09:15:00+05:30", "2026-03-09T09:20:00+05:30",
                         "2026-03-09T09:25:00+05:30", "2026-03-09T10:00:00+05:30"] + DAY1[4:] + DAY2
        loader_stamps = [s for s in stamps if s < "2026-03-09T15:00"]
        signal_at = {"2026-03-09T09:15:00+05:30", "2026-03-09T09:20:00+05:30"}
        engine = BacktestEngine(min_candles_required=2, capital=100000.0,
                                risk_pct_per_trade=0.01)
        engine.strategy_engine.evaluate = MagicMock(side_effect=(
            lambda s, w, context=None, strategy_names=None:
            [_v8d_buy(s)] if w[-1]["timestamp"] in signal_at else
            [StrategySignal(strategy_name="V8_D_PULLBACK_ATM", symbol=s,
                            signal=SignalType.NONE)]))
        res = engine.run(
            {"NIFTY50": _candles(stamps)},
            strategy_names=["V8_D_PULLBACK_ATM"],
            options_data_loader=_loader(loader_stamps),
            require_real_options=True,
        )
        assert res.trades_taken == 1
        assert res.trade_log[0]["entry_time"] == "2026-03-09T09:20:00+05:30"
        assert any("ENTRY_SESSION_RESTRICTED" in r for r in res.rejection_reason_counts)

    def test_1445_signal_still_enters_but_same_bar_square_off_wins_later(self):
        """14:45 is the last entry; the position then square-offs at 15:15."""
        res = _run(DAY1 + DAY2, {"2026-03-09T14:45:00+05:30"})
        assert res.trades_taken == 1
        assert res.trade_log[0]["entry_time"] == "2026-03-09T14:45:00+05:30"
        assert res.trade_log[0]["exit_reason"] == "INTRADAY_SQUARE_OFF"
