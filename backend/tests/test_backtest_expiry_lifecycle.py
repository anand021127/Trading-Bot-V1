"""Regression tests: option position expiry/position-lifecycle enforcement.

Background — 2026-09 forensic investigation of a REAL Upstox v3 backtest
(upstox_backtest_nifty50_banknifty_finnifty_+3_20250926_20260927.csv):

Trade SENSEX2630579900CE: entry 2026-03-05 15:25 IST (AFTER the 14:45
last-entry cutoff and on the contract's own expiry day), contract expiry
2026-03-05, exit 2026-09-25 15:25 with reason BACKTEST_END and gross P&L
exactly 0.0 — an expired contract was held ~6.5 months because:

  1. The engine's exit path had NO expiry awareness and silently skipped
     any real-option position whose candle was missing ("wait for real
     option candle or expiration" — expiration was never implemented),
     freezing the position until backtest end.
  2. Entries were never gated by the session window, so a signal at
     15:25 — which live execution could never have acted on — opened a
     position at all.

These tests pin the fixed behavior:
  - no open option position may survive past its actual contract expiry
  - BACKTEST_END must not override an earlier expiry
  - a position frozen by missing option candles must still be force-closed
    at expiry (never carried to BACKTEST_END months later)
  - entries must respect the session window (no 15:25 expiry-day entries)
  - regular exits (stop/target/square-off) still own expiry-day sessions
  - honest end-of-data BACKTEST_END closeout still works for positions
    that are genuinely alive at the last bar

All option candles used here are seeded into the real
HistoricalOptionsDataLoader (no synthetic price fabrication inside the
engine — the loader is the same verified-data seam production uses).
"""
from datetime import date
from unittest.mock import MagicMock

from backend.backtest.engine import BacktestEngine
from backend.backtest.options_data_layer import HistoricalOptionsDataLoader
from backend.strategy.signal import SignalType, StrategySignal

TS = {
    "d9_0945": "2026-03-09T09:45:00+05:30",
    "d9_1000": "2026-03-09T10:00:00+05:30",
    "d9_1005": "2026-03-09T10:05:00+05:30",
    "d9_1515": "2026-03-09T15:15:00+05:30",
    "d10_0945": "2026-03-10T09:45:00+05:30",
    "d10_1000": "2026-03-10T10:00:00+05:30",
    "d10_1525": "2026-03-10T15:25:00+05:30",  # anomaly bar: past 14:45 cutoff, at expiry close
    "d11_0945": "2026-03-11T09:45:00+05:30",
    "d11_1000": "2026-03-11T10:00:00+05:30",
    "d11_1515": "2026-03-11T15:15:00+05:30",
    "d12_0945": "2026-03-12T09:45:00+05:30",
    "d12_1000": "2026-03-12T10:00:00+05:30",
    "d12_1515": "2026-03-12T15:15:00+05:30",
}
CONTRACT_KEY = "BSE_FO|842104|10-03-2026"
EXPIRY = "2026-03-10"


def _spot_candles(timestamps, close=26000.0):
    return [
        {"timestamp": ts, "open": close - 40, "high": close + 50, "low": close - 60, "close": close, "volume": 10000}
        for ts in timestamps
    ]


def _seed_loader(opt_candles):
    """opt_candles: list of (timestamp, close) for the single SENSEX contract."""
    loader = HistoricalOptionsDataLoader(auto_load_cache=False)
    loader.load_contract_candles(
        underlying="SENSEX",
        expiry=EXPIRY,
        strike=79900.0,
        option_type="CE",
        instrument_key=CONTRACT_KEY,
        candles=[
            {
                "timestamp": ts,
                "open": close + 1,
                "high": close + 3,
                "low": close - 3,
                "close": close,
                "volume": 500,
            }
            for ts, close in opt_candles
        ],
        lot_size=20,
    )
    return loader


def _buy_signal(symbol):
    return StrategySignal(
        strategy_name="OPTION_PREMIUM",
        symbol=symbol,
        signal=SignalType.BUY,
        confidence=99.0,
        entry_price=100.0,
        stop_loss=80.0,
        target=160.0,
    )


def _equity_buy_signal(symbol):
    return StrategySignal(
        strategy_name="OPTION_PREMIUM",
        symbol=symbol,
        signal=SignalType.BUY,
        confidence=99.0,
        entry_price=26000.0,
        stop_loss=25800.0,
        target=26300.0,
    )


def _make_engine(signal_at, *, require_real_options=True, opt_candles=None, enforce_entry_session_window=True):
    """Engine whose strategy emits a BUY exactly at timestamps in `signal_at`
    (a set of timestamp strings) and NONE elsewhere."""

    def fake_eval(symbol, window, context=None, strategy_names=None):
        ts = window[-1]["timestamp"]
        if ts in signal_at:
            sig = _buy_signal(symbol) if require_real_options else _equity_buy_signal(symbol)
        else:
            sig = StrategySignal(strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.NONE)
        return [sig]

    engine = BacktestEngine(
        min_candles_required=2,
        max_simultaneous_positions=6,
        enforce_entry_session_window=enforce_entry_session_window,
    )
    engine.strategy_engine.evaluate = MagicMock(side_effect=fake_eval)
    loader = None
    if require_real_options:
        loader = _seed_loader(opt_candles or [])
    return engine, loader


class TestExpiryLifecycleEnforcement:
    # ── Core regression: BACKTEST_END must never override expiry ──────────

    def test_position_open_after_expiry_is_force_closed_at_expiry(self):
        """A position still open after its expiry date must be closed dated at
        expiry (15:25 IST) with EXPIRY_FORCED_CLOSE — never BACKTEST_END."""
        candles = _spot_candles([TS["d9_0945"], TS["d9_1000"], TS["d9_1005"], TS["d9_1515"],
                                 TS["d10_0945"], TS["d10_1000"], TS["d10_1525"],
                                 TS["d11_0945"], TS["d11_1000"], TS["d11_1515"],
                                 TS["d12_0945"], TS["d12_1000"], TS["d12_1515"]], close=79900.0)
        engine, loader = _make_engine(
            {TS["d10_0945"]},
            opt_candles=[(TS["d10_0945"], 60.0)],  # entry candle only; data "disappears" after
        )
        result = engine.run(
            {"SENSEX": candles},
            strategy_names=["OPTION_PREMIUM"],
            options_data_loader=loader,
            require_real_options=True,
        )

        assert result.trades_taken == 1
        trade = result.trade_log[0]
        # The position was still open on 2026-03-11 (after the 2026-03-10
        # expiry): the guard must close it dated at expiry, not at 03-11 and
        # certainly not at BACKTEST_END.
        assert trade["exit_reason"] == "EXPIRY_FORCED_CLOSE"
        assert trade["exit_time"] == "2026-03-10T15:25:00+05:30"
        assert trade["expiry"] == EXPIRY
        assert not any(t["exit_reason"] == "BACKTEST_END" for t in result.trade_log)
        assert result.lifecycle_violations_prevented == 1
        assert result.positions_forced_expiry_closed == 1
        # Exit price must be an actually-observed option price (entry candle
        # close), never a fabricated terminal value or the underlying spot.
        assert trade["exit_price"] == 60.0

    def test_frozen_position_missing_candles_still_closed_at_expiry(self):
        """Entry one day before expiry with ALL later option candles missing
        (the SENSEX2630579900CE data-gap scenario): the position must not be
        frozen into BACKTEST_END — it is force-closed at expiry."""
        candles = _spot_candles([TS["d9_0945"], TS["d9_1000"], TS["d9_1005"], TS["d9_1515"],
                                 TS["d10_0945"], TS["d10_1000"], TS["d10_1525"],
                                 TS["d11_0945"], TS["d11_1000"], TS["d11_1515"],
                                 TS["d12_0945"], TS["d12_1000"], TS["d12_1515"]], close=79900.0)
        engine, loader = _make_engine(
            {TS["d9_1005"]},  # entry on 03-09 (day before expiry)
            opt_candles=[(TS["d9_1005"], 100.0)],  # then every option candle is missing
        )
        result = engine.run(
            {"SENSEX": candles},
            strategy_names=["OPTION_PREMIUM"],
            options_data_loader=loader,
            require_real_options=True,
        )

        assert result.trades_taken == 1
        trade = result.trade_log[0]
        assert trade["exit_reason"] == "EXPIRY_FORCED_CLOSE"
        assert trade["exit_time"] == "2026-03-10T15:25:00+05:30"  # expiry close, not backtest end
        assert not any(t["exit_reason"] == "BACKTEST_END" for t in result.trade_log)
        assert result.positions_forced_expiry_closed == 1
        assert trade["exit_price"] == 100.0  # last observed option price

    def test_regular_exit_owns_expiry_day_before_deadline(self):
        """A stop-loss on expiry day (before the 15:25 deadline) exits via the
        normal path — the lifecycle guard must not interfere."""
        candles = _spot_candles([TS["d9_0945"], TS["d9_1000"], TS["d9_1005"], TS["d9_1515"],
                                 TS["d10_0945"], TS["d10_1000"], TS["d10_1525"],
                                 TS["d11_0945"], TS["d11_1000"], TS["d11_1515"],
                                 TS["d12_0945"], TS["d12_1000"], TS["d12_1515"]], close=79900.0)
        engine, loader = _make_engine(
            {TS["d9_1005"]},
            opt_candles=[(TS["d9_1005"], 100.0), (TS["d10_0945"], 50.0)],  # 03-10 candle gaps through stop
        )
        result = engine.run(
            {"SENSEX": candles},
            strategy_names=["OPTION_PREMIUM"],
            options_data_loader=loader,
            require_real_options=True,
        )

        assert result.trades_taken == 1
        trade = result.trade_log[0]
        assert trade["exit_reason"] == "STOP_LOSS_HIT"
        assert trade["exit_time"] == TS["d10_0945"]  # well before the 15:25 expiry deadline
        assert result.lifecycle_violations_prevented == 0
        assert result.positions_forced_expiry_closed == 0

    def test_honest_backtest_end_still_works_for_alive_positions(self):
        """A position genuinely alive at the last bar (well before expiry)
        still closes with BACKTEST_END at the final timestamp — honest."""
        day13 = ["2026-03-13T09:40:00+05:30", "2026-03-13T09:45:00+05:30",
                 "2026-03-13T10:00:00+05:30", "2026-03-13T14:50:00+05:30"]
        candles = _spot_candles([TS["d9_0945"], TS["d9_1000"], TS["d9_1005"], TS["d9_1515"],
                                 TS["d10_0945"], TS["d10_1000"], TS["d10_1525"],
                                 TS["d11_0945"], TS["d11_1000"], TS["d11_1515"],
                                 TS["d12_0945"], TS["d12_1000"], TS["d12_1515"]] + day13)
        engine, loader = _make_engine(
            {"2026-03-13T09:40:00+05:30"},
            require_real_options=False,  # equity path; expiry untouched
        )
        result = engine.run(
            {"SENSEX": candles},
            strategy_names=["OPTION_PREMIUM"],
            options_data_loader=loader,
            require_real_options=False,
        )

        assert result.trades_taken == 1
        trade = result.trade_log[0]
        assert trade["exit_reason"] == "BACKTEST_END"
        assert trade["exit_time"] == "2026-03-13T14:50:00+05:30"
        assert result.lifecycle_violations_prevented == 0


class TestEntrySessionWindowParity:
    def test_no_entry_after_last_entry_cutoff(self):
        """The 15:25 signal (past the 14:45 last-entry cutoff, on expiry day)
        must never open a position — live execution could not have taken it."""
        candles = _spot_candles([TS["d9_0945"], TS["d9_1000"], TS["d9_1005"], TS["d9_1515"],
                                 TS["d10_0945"], TS["d10_1000"], TS["d10_1525"],
                                 TS["d11_0945"], TS["d11_1000"], TS["d11_1515"],
                                 TS["d12_0945"], TS["d12_1000"], TS["d12_1515"]])
        engine, loader = _make_engine({TS["d10_1525"]})
        result = engine.run(
            {"SENSEX": candles},
            strategy_names=["OPTION_PREMIUM"],
            options_data_loader=loader,
            require_real_options=True,
        )

        assert result.trades_taken == 0
        assert result.entry_session_rejections >= 1
        assert any("ENTRY_SESSION_RESTRICTED" in r for r in result.rejection_reason_counts)

    def test_no_entry_after_last_entry_cutoff_equity_path(self):
        """Same gate on the spot/equity execution path (mock tests only)."""
        candles = _spot_candles([TS["d9_0945"], TS["d9_1000"], TS["d9_1005"], TS["d9_1515"],
                                 TS["d10_0945"], TS["d10_1000"], TS["d10_1525"],
                                 TS["d11_0945"], TS["d11_1000"], TS["d11_1515"],
                                 TS["d12_0945"], TS["d12_1000"], TS["d12_1515"]])
        engine, loader = _make_engine({TS["d10_1525"]}, require_real_options=False)
        result = engine.run(
            {"SENSEX": candles},
            strategy_names=["OPTION_PREMIUM"],
            options_data_loader=loader,
            require_real_options=False,
        )

        assert result.trades_taken == 0
        assert result.entry_session_rejections >= 1

    def test_session_gate_is_opt_out_for_legacy_compatibility(self):
        """Documented toggle: enforce_entry_session_window=False restores the
        legacy (un-gated) behavior. Production keeps the gate ON."""
        candles = _spot_candles([TS["d9_0945"], TS["d9_1000"], TS["d9_1005"], TS["d9_1515"],
                                 TS["d10_0945"], TS["d10_1000"], TS["d10_1525"],
                                 TS["d11_0945"], TS["d11_1000"], TS["d11_1515"],
                                 TS["d12_0945"], TS["d12_1000"], TS["d12_1515"]])
        engine, loader = _make_engine(
            {TS["d10_1525"]},
            require_real_options=False,
            enforce_entry_session_window=False,
        )
        result = engine.run(
            {"SENSEX": candles},
            strategy_names=["OPTION_PREMIUM"],
            options_data_loader=loader,
            require_real_options=False,
        )

        assert result.trades_taken >= 1  # gate disabled -> 15:25 entry allowed (legacy)


class TestNormalIntradayBehaviorUnchanged:
    def test_normal_intraday_trade_still_closes_at_square_off(self):
        """A normal intraday equity trade (entry 09:45) still square-offs at
        15:15/15:25 — the lifecycle work changed nothing here."""
        candles = _spot_candles([TS["d9_0945"], TS["d9_1000"], TS["d9_1005"], TS["d9_1515"],
                                 TS["d10_0945"], TS["d10_1000"], TS["d10_1525"],
                                 TS["d11_0945"], TS["d11_1000"], TS["d11_1515"],
                                 TS["d12_0945"], TS["d12_1000"], TS["d12_1515"]])
        engine, loader = _make_engine(
            {TS["d10_0945"], TS["d10_1000"]},  # re-signal after square-off is fine
            require_real_options=False,
        )
        result = engine.run(
            {"SENSEX": candles},
            strategy_names=["OPTION_PREMIUM"],
            options_data_loader=loader,
            require_real_options=False,
        )

        assert result.trades_taken == 1
        trade = result.trade_log[0]
        assert trade["exit_reason"] == "INTRADAY_SQUARE_OFF"
        assert trade["exit_time"] == TS["d10_1525"]  # first bar at/after 15:15 square-off
