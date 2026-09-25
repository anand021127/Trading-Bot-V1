"""ONE COMMON TRADE METADATA MODEL — regression tests.

Every execution mode (PAPER, LIVE-mocked, BACKTEST) must record the same
trade metadata contract (backend/domain/trade_metadata.py):

    underlying_symbol, option_type, strike_price, expiry, instrument_key,
    entry_price, exit_price, quantity (executed), lot_size, capital_used
    (= entry_price x executed_quantity, never allocation/max-risk/account
    capital), entry/exit timestamps, strategy, status, trade/order/signal ids.

Rules enforced here:
- nothing is invented: metadata that genuinely does not exist stays NULL
  and renders as "N/A / Historical metadata unavailable";
- capital_used is ALWAYS entry_price x executed quantity;
- the trades table is the single schema (paper == live == backtest);
- no real Upstox order is ever placed (offline env + mocks only).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.database.db_manager import DatabaseManager  # noqa: E402
from backend.database.models import Trade  # noqa: E402
from backend.domain.trade_metadata import (  # noqa: E402
    TRADE_METADATA_FIELDS,
    compute_capital_used,
    historical_display_value,
    normalize_trade_metadata,
)
from backend.paper.paper_broker import PaperBroker  # noqa: E402

HISTORICAL_NOTE = "N/A / Historical metadata unavailable"

# Authoritative contract metadata used across the suite (nothing here is
# derived from a symbol name or the current date — it stands in for what
# the broker/chain/historical resolver actually returned).
_CONTRACT = {
    "instrument_key": "NSE_FO|69780",
    "underlying": "NIFTY50",
    "option_type": "CE",
    "strike": 24800.0,
    "expiry": "2027-01-07",
    "lot_size": 75,
    "premium": 120.0,
    "quantity": 75,
    "stop_loss": 105.0,
    "target": 168.0,
    "strategy": "V8_D_PULLBACK_ATM",
}


def _db() -> DatabaseManager:
    return DatabaseManager(db_path=os.path.join(
        tempfile.mkdtemp(prefix="trademeta_"), f"t_{uuid.uuid4().hex}.db"))


def _make_runtime(db, broker=None, **env_over):
    """Paper runtime on a temp DB with offline env defaults (same pattern as
    test_production_hardening_regression.py)."""
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
    """Submit an entry as if mid-morning (tests may run after EOD cutoff)."""
    morning = rt.now_fn().replace(hour=10, minute=0, second=0, microsecond=0)
    with mock.patch.object(rt, "now_fn", return_value=morning):
        return rt.submit_entry(dict(signal or _CONTRACT))


# ────────────────────────────────────────────────────────────────────────────
# Contract module itself
# ────────────────────────────────────────────────────────────────────────────

class TestTradeMetadataContract:
    def test_capital_used_is_entry_price_times_executed_quantity(self):
        assert compute_capital_used(125.40, 75) == 9405.0

    def test_capital_used_rejects_invalid_inputs_instead_of_inventing(self):
        assert compute_capital_used(0, 75) is None          # invalid price
        assert compute_capital_used(-5, 75) is None         # negative price
        assert compute_capital_used(120.0, 0) is None       # zero quantity
        assert compute_capital_used(None, 75) is None       # missing price
        assert compute_capital_used(120.0, None) is None    # missing qty
        assert compute_capital_used("abc", 75) is None      # non-numeric
        assert compute_capital_used(float("inf"), 75) is None

    def test_normalize_maps_aliases_and_validates_option_type(self):
        m = normalize_trade_metadata({
            "underlying": "NIFTY50", "option_type": "pe", "strike": 24500,
            "expiry_date": "2026-10-29", "lot_size": "75", "quantity": 75,
        })
        assert m["underlying_symbol"] == "NIFTY50"
        assert m["option_type"] == "PE"
        assert m["strike_price"] == 24500.0
        assert m["expiry"] == "2026-10-29"
        assert m["lot_size"] == 75

    def test_normalize_fails_safe_on_missing_or_invalid_metadata(self):
        # missing strike / expiry / lot size → None, never guessed
        m = normalize_trade_metadata({"underlying": "NIFTY50", "quantity": 75})
        assert m["strike_price"] is None
        assert m["expiry"] is None
        assert m["lot_size"] is None
        assert m["capital_used"] is None
        # invalid option kind is not persisted as-is
        assert normalize_trade_metadata({"option_type": "XX"})["option_type"] is None
        # invalid lot size (0/negative) is dropped, not stored
        assert normalize_trade_metadata({"lot_size": 0})["lot_size"] is None
        assert normalize_trade_metadata({"lot_size": -75})["lot_size"] is None
        # non-numeric strike dropped
        assert normalize_trade_metadata({"strike": "NIFTY"})["strike_price"] is None

    def test_historical_display_note_never_invents_values(self):
        assert historical_display_value(None) == HISTORICAL_NOTE
        assert historical_display_value("") == HISTORICAL_NOTE
        assert historical_display_value(24800.0) == 24800.0


# ────────────────────────────────────────────────────────────────────────────
# PAPER (1-7 of the required list)
# ────────────────────────────────────────────────────────────────────────────

class TestPaperTradeMetadata:
    def test_paper_entry_persists_full_metadata(self):
        db = _db()
        rt = next(_make_runtime(db))
        res = _submit(rt)
        assert res.accepted, getattr(res, "reason", None)
        row = db.list_trades()[0]
        assert row["underlying_symbol"] == "NIFTY50"
        assert row["option_type"] == "CE"
        assert row["strike_price"] == 24800.0
        assert row["expiry"] == "2027-01-07"
        assert row["instrument_key"] == "NSE_FO|69780"
        assert row["quantity"] == 75
        assert row["lot_size"] == 75
        assert row["capital_used"] == 9000.0
        assert row["entry_price"] == 120.0
        assert row["entry_time"], "entry timestamp missing"
        assert row["strategy"] == "V8_D_PULLBACK_ATM"
        assert row["status"] == "filled"
        assert row["signal_id"], "signal_id missing"
        assert row["order_id"], "order_id missing"
        db.close()

    def test_paper_capital_uses_simulated_fill_price_not_requested(self):
        db = _db()
        rt = next(_make_runtime(db))
        # Simulated fill executes at a different price than requested —
        # metadata/capital must reflect the ACTUAL fill (order.avg_price).
        orig_place = rt.broker.place_order

        def fill_at_executed_price(**kw):
            kw["price"] = 123.5
            return orig_place(**kw)

        rt.broker.place_order = fill_at_executed_price
        _submit(rt)
        row = db.list_trades()[0]
        assert row["entry_price"] == 123.5, "entry price must be the executed fill"
        assert row["capital_used"] == round(123.5 * 75, 2)
        assert row["capital_used"] != 9000.0, "capital must not use the requested premium"
        db.close()

    def test_paper_partial_fill_capital_uses_executed_quantity(self):
        db = _db()
        rt = next(_make_runtime(db))
        rt.broker.next_fill_mode = "half"  # 75 -> 37 executed
        _submit(rt)
        row = db.list_trades()[0]
        assert row["quantity"] == 37
        assert row["capital_used"] == round(120.0 * 37, 2)
        assert row["capital_used"] != 120.0 * 75, "capital must use EXECUTED qty"
        db.close()

    def test_paper_exit_preserves_entry_metadata_and_persists_pnl(self):
        db = _db()
        rt = next(_make_runtime(db))
        _submit(rt)
        trade_id = db.list_trades()[0]["id"]
        summary = rt.on_option_quote("NSE_FO|69780", 170.0)  # >= target 168
        assert summary is not None
        row = db.get_trade(trade_id)
        assert row["exit_price"] == 170.0
        assert row["exit_time"], "exit timestamp missing"
        assert row["status"] == "closed"
        assert row["net_pnl"] is not None, "net P&L must persist on exit"
        assert row["gross_pnl"] is not None
        assert row["brokerage"] is not None and row["stt"] is not None
        # Entry metadata preserved untouched by the exit write
        assert row["strike_price"] == 24800.0
        assert row["option_type"] == "CE"
        assert row["expiry"] == "2027-01-07"
        assert row["lot_size"] == 75
        assert row["capital_used"] == 9000.0
        db.close()

    def test_paper_restart_preserves_full_position_metadata(self):
        db = _db()
        rt = next(_make_runtime(db))
        _submit(rt)
        trade_id = db.list_trades()[0]["id"]
        db.close()
        db2 = DatabaseManager(db_path=db.db_path)
        rt2 = next(_make_runtime(db2))
        pos = rt2.broker.positions.get("NSE_FO|69780")
        assert pos is not None, "position not hydrated after restart"
        assert pos["strike"] == 24800.0
        assert pos["option_type"] == "CE"
        assert pos["expiry"] == "2027-01-07"
        assert pos["quantity"] == 75
        assert pos["lot_size"] == 75
        assert pos["average_price"] == 120.0
        assert pos["trade_id"] == trade_id
        db2.close()

    def test_paper_zero_quantity_is_refused_safely(self):
        db = _db()
        rt = next(_make_runtime(db))
        bad = dict(_CONTRACT, quantity=0)
        res = _submit(rt, bad)
        assert not res.accepted
        assert db.list_trades() == []
        db.close()

    def test_paper_non_lot_multiple_quantity_is_refused(self):
        db = _db()
        rt = next(_make_runtime(db))
        res = _submit(rt, dict(_CONTRACT, quantity=70))  # not multiple of 75
        assert not res.accepted
        assert db.list_trades() == []
        db.close()

    def test_paper_duplicate_signal_does_not_duplicate_metadata_rows(self):
        db = _db()
        rt = next(_make_runtime(db))
        sig = dict(_CONTRACT)
        r1 = _submit(rt, sig)
        r2 = _submit(rt, sig)  # identical signal -> pipeline duplicate guard
        assert r1.accepted
        assert not r2.accepted
        rows = db.list_trades()
        assert len(rows) == 1, "duplicate execution created a second trade row"
        db.close()


# ────────────────────────────────────────────────────────────────────────────
# LIVE (mocked broker — no real orders possible)
# ────────────────────────────────────────────────────────────────────────────

def _live_engine(db_path: str):
    """TradingEngine wired to a paper-mode OrderManager (no real broker
    reachable) — the exact harness used by test_single_execution_path.py.
    Nothing here can place a real Upstox order."""
    import backend.strategy.trading_engine as te_mod
    te_mod.settings.mode = "paper"
    te_mod.settings.strategy.name = "V8_D_PULLBACK_ATM"
    te_mod.settings.order.product = "I"
    te_mod.settings.risk.max_risk_per_trade_pct = 0.025
    te_mod.settings.capital.total = 100000.0
    te_mod.settings.capital.max_allocation_per_trade = 0.18
    from backend.orders.order_manager import OrderManager
    from backend.strategy.trading_engine import TradingEngine
    db = DatabaseManager(db_path=db_path)
    client = MagicMock()
    # Deterministic quote for the paper fill model (a bare MagicMock would
    # coerce to float 1.0 and silently distort the fill price).
    client.get_quote_by_instrument_key = lambda ik: {"ltp": 120.0, "bid_price": None, "ask_price": None, "has_data": True}
    om = OrderManager(client=client, paper_mode=True, default_product="I")
    eng = TradingEngine(db_manager=db, client=client, order_manager=om)
    eng._position_mismatch = False
    eng._reconciled = True
    eng.risk_manager.check_lot_risk = lambda **kw: (True, "ok")
    eng.risk_manager.can_take_trade = lambda *a, **k: (True, "ok")
    eng.risk_manager.check_exposure = lambda **kw: (True, "ok")
    return eng, db


def _live_signal():
    from backend.strategy.signal import SignalType, StrategySignal
    sig = StrategySignal(
        strategy_name="V8_D_PULLBACK_ATM",
        symbol="NIFTY50",
        signal=SignalType.BUY,
        confidence=80.0,
        entry_price=120.0,
        stop_loss=105.0,
        target=168.0,
        generated_at="2026-09-24T10:00:00+05:30",
    )
    sig.indicators = {
        # Authoritative broker-resolved contract (same object the live path uses)
        "selected_contract": {
            "option_type": "CE",
            "strike": 24800,
            "instrument_key": "NSE_FO|69780",
            "lot_size": 75,
            "freeze_quantity": 1800,
            "expiry": "2027-01-07",
        },
        "expiry_date": "2027-01-07",
        "spot_price": 24810.0,
        "quote_age_seconds": 1,
        "atr": 6.0,
    }
    return sig


class TestLiveMockedTradeMetadata:
    def test_live_mocked_entry_records_contract_metadata_and_capital(self):
        path = os.path.join(tempfile.mkdtemp(), "live_meta.db")
        eng, db = _live_engine(path)
        with mock.patch.object(eng.position_sizer, "calculate", return_value=75):
            trade_id = eng.execute_multi_signal(_live_signal())
        assert trade_id is not None, "mocked live entry did not execute"
        row = db.get_trade(trade_id)
        assert row["underlying_symbol"] == "NIFTY50"
        assert row["option_type"] == "CE"
        assert row["strike_price"] == 24800.0
        assert row["expiry"] == "2027-01-07"
        assert row["instrument_key"] == "NSE_FO|69780"
        assert row["lot_size"] == 75
        assert row["quantity"] == 75
        assert row["capital_used"] == round(row["entry_price"] * row["quantity"], 2)
        assert row["order_id"], "live order id must persist"
        db.close()

    def test_live_mocked_capital_uses_actual_fill_price(self):
        path = os.path.join(tempfile.mkdtemp(), "live_meta2.db")
        eng, db = _live_engine(path)
        # Paper fill model applies 0.1% slippage over the quote — capital must
        # be computed from that ACTUAL fill, not the requested premium.
        with mock.patch.object(eng.position_sizer, "calculate", return_value=75):
            trade_id = eng.execute_multi_signal(_live_signal())
        row = db.get_trade(trade_id)
        assert row["entry_price"] > 0
        assert row["capital_used"] == round(row["entry_price"] * row["quantity"], 2)
        db.close()

    def test_live_mocked_partial_fill_uses_executed_quantity(self):
        path = os.path.join(tempfile.mkdtemp(), "live_meta3.db")
        eng, db = _live_engine(path)
        eng.order_manager.place_order = MagicMock(side_effect=self._partial_fill_order)
        with mock.patch.object(eng.position_sizer, "calculate", return_value=75):
            trade_id = eng.execute_multi_signal(_live_signal())
        assert trade_id is not None
        row = db.get_trade(trade_id)
        assert row["quantity"] == 25, "partial fill must persist EXECUTED qty"
        assert row["capital_used"] == round(row["entry_price"] * 25, 2)
        db.close()

    @staticmethod
    def _partial_fill_order(request):
        from backend.orders.order_models import Order, OrderStatus
        return Order(
            id="LIVE-MOCK-PARTIAL",
            symbol=request.symbol,
            status=OrderStatus.PARTIALLY_FILLED,
            filled_quantity=25,
            remaining_quantity=50,
            quantity=75,
            price=120.5,
            average_price=120.5,
        )

    def test_live_mocked_position_detail_exposes_metadata(self):
        path = os.path.join(tempfile.mkdtemp(), "live_meta4.db")
        eng, db = _live_engine(path)
        with mock.patch.object(eng.position_sizer, "calculate", return_value=75):
            eng.execute_multi_signal(_live_signal())
        details = eng.get_open_positions_detail()
        assert len(details) == 1
        d = details[0]
        assert d["underlying_symbol"] == "NIFTY50"
        assert d["option_type"] == "CE"
        assert d["strike_price"] == 24800
        assert d["lot_size"] == 75
        assert d["capital_used"] == round(d["entry_price"] * d["quantity"], 2)
        db.close()

    def test_live_mode_still_blocks_real_orders_without_token(self):
        """Paper-live safety: TRADING_MODE=live with an unusable token is
        refused by the pipeline (require_live_token) — no order path runs."""
        os.environ["TRADING_BOT_OFFLINE_TESTS"] = "1"
        os.environ.pop("UPSTOX_ACCESS_TOKEN", None)
        import backend.strategy.trading_engine as te_mod
        te_mod.settings.mode = "live"
        te_mod.settings.order.product = "I"
        from backend.orders.order_manager import OrderManager
        from backend.strategy.trading_engine import TradingEngine
        db = DatabaseManager(db_path=os.path.join(tempfile.mkdtemp(), "live_safe.db"))
        client = MagicMock()
        client.get_quote_by_instrument_key = lambda ik: {"ltp": 120.0, "bid_price": None, "ask_price": None, "has_data": True}
        om = OrderManager(client=client, paper_mode=False, default_product="I")
        eng = TradingEngine(db_manager=db, client=client, order_manager=om)
        eng._position_mismatch = False
        eng._reconciled = True
        eng.risk_manager.check_lot_risk = lambda **kw: (True, "ok")
        eng.risk_manager.can_take_trade = lambda *a, **k: (True, "ok")
        eng.risk_manager.check_exposure = lambda **kw: (True, "ok")
        with mock.patch.object(eng.position_sizer, "calculate", return_value=75):
            trade_id = eng.execute_multi_signal(_live_signal())
        assert trade_id is None, "live mode without a usable token must not trade"
        assert db.list_trades() == []
        te_mod.settings.mode = "paper"
        db.close()


# ────────────────────────────────────────────────────────────────────────────
# BACKTEST
# ────────────────────────────────────────────────────────────────────────────

def _backtest_with_real_contract():
    from backend.backtest.engine import BacktestEngine
    from backend.backtest.options_data_layer import HistoricalOptionsDataLoader
    from backend.strategy.signal import SignalType, StrategySignal
    from unittest.mock import MagicMock as _MM

    loader = HistoricalOptionsDataLoader()
    loader.load_contract_candles(
        underlying="NIFTY50",
        expiry="2024-06-27",
        strike=24500.0,
        option_type="CE",
        instrument_key="NSE_FO|NIFTY2462724500CE",
        candles=[
            {"timestamp": "2024-06-25T09:15:00", "open": 180.0, "high": 195.0, "low": 175.0, "close": 190.0, "volume": 50000, "oi": 120000},
            {"timestamp": "2024-06-25T09:20:00", "open": 190.0, "high": 220.0, "low": 185.0, "close": 215.0, "volume": 75000, "oi": 125000},
            {"timestamp": "2024-06-25T09:25:00", "open": 215.0, "high": 230.0, "low": 210.0, "close": 225.0, "volume": 60000, "oi": 130000},
        ],
        lot_size=75,  # actual NIFTY contract lot for that period — from data, not hardcoded per-strike
    )
    engine = BacktestEngine(min_candles_required=2)
    spot_candles = [
        {"timestamp": "2024-06-25T09:10:00", "open": 24490, "high": 24510, "low": 24480, "close": 24500, "volume": 1000000},
        {"timestamp": "2024-06-25T09:15:00", "open": 24500, "high": 24520, "low": 24495, "close": 24502, "volume": 1200000},
        {"timestamp": "2024-06-25T09:20:00", "open": 24502, "high": 24515, "low": 24498, "close": 24505, "volume": 1500000},
        {"timestamp": "2024-06-25T09:25:00", "open": 24505, "high": 24530, "low": 24500, "close": 24520, "volume": 1100000},
    ]

    def fake_evaluate(symbol, window, context=None, strategy_names=None):
        curr = window[-1]
        if curr["timestamp"] == "2024-06-25T09:20:00":
            return [StrategySignal(
                strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.BUY,
                entry_price=curr["close"], stop_loss=24450.0, target=24600.0, confidence=0.85,
            )]
        return [StrategySignal(strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.NONE)]

    engine.strategy_engine.evaluate = _MM(side_effect=fake_evaluate)
    result = engine.run(
        {"NIFTY50": spot_candles},
        strategy_names=["OPTION_PREMIUM"],
        options_data_loader=loader,
        require_real_options=True,
    )
    return result


class TestBacktestTradeMetadata:
    def test_backtest_trade_carries_full_contract_metadata(self):
        result = _backtest_with_real_contract()
        assert result.trades_taken >= 1
        trade = result.trade_log[0]
        assert trade["underlying_symbol"] == "NIFTY50"
        assert trade["option_type"] == "CE"
        assert trade["strike_price"] == 24500.0
        assert trade["expiry"] == "2024-06-27"
        assert trade["instrument_key"] == "NSE_FO|NIFTY2462724500CE"
        assert trade["quantity"] > 0
        assert trade["lot_size"] == 75

    def test_backtest_capital_used_is_entry_price_times_quantity(self):
        result = _backtest_with_real_contract()
        trade = result.trade_log[0]
        assert trade["capital_used"] == round(float(trade["entry_price"]) * int(trade["quantity"]), 2)
        # Never the configured backtest account capital
        assert trade["capital_used"] != 100000.0
        assert trade["capital_used"] < 50000  # a single option premium outlay

    def test_backtest_status_and_timestamps_exposed(self):
        result = _backtest_with_real_contract()
        trade = result.trade_log[0]
        assert trade["status"] == "closed"
        assert trade["entry_timestamp"] == trade["entry_time"]
        assert trade["exit_timestamp"]

    def test_backtest_synthetic_contract_fields_render_na(self):
        """Without a resolved historical contract, metadata is empty/None —
        the display layer shows N/A rather than inventing a strike."""
        from backend.backtest.engine import BacktestTrade
        t = BacktestTrade(
            symbol="NIFTY50", strategy="OPTION_PREMIUM",
            entry_time="2024-06-25T09:20:00", exit_time="2024-06-25T10:00:00",
            entry_price=100.0, exit_price=110.0, quantity=75, exit_reason="TARGET",
            gross_pnl=750.0, net_pnl=720.0, charges=30.0, confidence=0.8,
        )
        d = t.to_dict()
        assert d["strike_price"] is None
        assert d["option_type"] == ""
        assert d["capital_used"] == 7500.0  # still entry x executed qty
        assert d["status"] == "closed"


# ────────────────────────────────────────────────────────────────────────────
# DATABASE MIGRATION / HISTORICAL COMPATIBILITY
# ────────────────────────────────────────────────────────────────────────────

class TestMigrationAndHistoricalCompatibility:
    def test_fresh_db_has_all_metadata_columns(self):
        db = _db()
        cols = {r["name"] for r in db._connect().execute("PRAGMA table_info(trades)").fetchall()}
        missing = {"underlying_symbol", "option_type", "strike_price", "expiry",
                   "instrument_key", "lot_size", "capital_used", "order_id", "signal_id"} - cols
        assert not missing, f"missing metadata columns: {missing}"
        db.close()

    def test_migration_preserves_existing_historical_trades(self):
        """A pre-metadata database (old schema + old rows) migrates cleanly:
        new columns exist, historical rows keep their data, metadata stays
        NULL (displayed as N/A), nothing deleted or rewritten."""
        legacy = os.path.join(tempfile.mkdtemp(), "legacy.db")
        conn = sqlite3.connect(legacy)
        conn.execute(
            "CREATE TABLE trades (id TEXT PRIMARY KEY, symbol TEXT, side TEXT, quantity INTEGER,"
            " price REAL, timestamp TEXT, strategy TEXT, status TEXT, pnl REAL, notes TEXT)"
        )
        conn.execute(
            "INSERT INTO trades VALUES ('hist-1','NIFTY','BUY',75,120.0,"
            " '2024-05-10T09:30:00+00:00','V8_D','closed',500.0,'old row')"
        )
        conn.commit()
        conn.close()

        db = DatabaseManager(db_path=legacy)  # triggers migration
        rows = db.list_trades()
        assert len(rows) == 1, "historical trade must survive migration"
        row = rows[0]
        assert row["id"] == "hist-1"
        assert row["symbol"] == "NIFTY"
        assert row["quantity"] == 75
        assert row["price"] == 120.0
        assert row["pnl"] == 500.0
        # No invented metadata for the historical row
        assert row["strike_price"] is None
        assert row["option_type"] is None
        assert row["expiry"] is None
        assert row["lot_size"] is None
        assert row["capital_used"] is None
        assert historical_display_value(row["strike_price"]) == HISTORICAL_NOTE
        # Migration is restart-safe / idempotent
        db.close()
        db2 = DatabaseManager(db_path=legacy)
        assert len(db2.list_trades()) == 1
        db2.close()

    def test_api_row_shape_serves_full_metadata_contract(self):
        """/api/trades row contract: every canonical metadata field present
        (None for absent) so the frontend never needs a second schema."""
        db = _db()
        from backend.api.routers.trading import _row_to_dict
        db.insert_trade(Trade(
            id="api-1", symbol="NIFTY50", side="BUY", quantity=75, price=120.0,
            timestamp=datetime.now(timezone.utc), strategy="V8_D_PULLBACK_ATM",
            status="filled",
            trade_metadata={"underlying_symbol": "NIFTY50", "option_type": "CE",
                            "strike_price": 24800.0, "expiry": "2027-01-07",
                            "instrument_key": "NSE_FO|69780", "lot_size": 75,
                            "capital_used": 9000.0},
        ))
        d = _row_to_dict(db.list_trades()[0])
        for field in TRADE_METADATA_FIELDS:
            assert field in d, f"{field} missing from API row"
        assert d["capital_used"] == 9000.0
        assert d["strike_price"] == 24800.0
        db.close()

    def test_get_trade_returns_metadata_columns(self):
        db = _db()
        db.insert_trade(Trade(
            id="gt-1", symbol="BANKNIFTY", side="BUY", quantity=35, price=210.0,
            timestamp=datetime.now(timezone.utc), strategy="V8_D_PULLBACK_ATM",
            status="filled",
            trade_metadata={"underlying_symbol": "BANKNIFTY", "option_type": "PE",
                            "strike_price": 51200.0, "expiry": "2026-10-29",
                            "instrument_key": "NSE_FO|99999", "lot_size": 35,
                            "capital_used": 7350.0},
        ))
        row = db.get_trade("gt-1")
        assert row["strike_price"] == 51200.0
        assert row["option_type"] == "PE"
        assert row["capital_used"] == 7350.0
        db.close()


# ────────────────────────────────────────────────────────────────────────────
# WORKER RESTART / DUPLICATES / EDGE SAFETY
# ────────────────────────────────────────────────────────────────────────────

class TestRestartDupAndEdgeSafety:
    def test_worker_restart_preserves_trade_metadata_and_exit_still_works(self):
        db = _db()
        rt = next(_make_runtime(db))
        _submit(rt)
        trade_id = db.list_trades()[0]["id"]
        db.close()

        db2 = DatabaseManager(db_path=db.db_path)
        rt2 = next(_make_runtime(db2))
        # Worker restart flow: hydrate from durable ledger, then exit fires
        rt2._hydrate_paper_broker_from_db()
        summary = rt2.on_option_quote("NSE_FO|69780", 100.0)  # below restored SL
        assert summary is not None and summary["exit_reason"] == "STOP_LOSS"
        row = db2.get_trade(trade_id)
        assert row["status"] == "closed"
        assert row["strike_price"] == 24800.0, "metadata lost across restart"
        assert row["capital_used"] == 9000.0
        # Idempotent: no second exit
        assert rt2.on_option_quote("NSE_FO|69780", 200.0) is None
        assert len(db2.list_trades()) == 1
        db2.close()

    def test_invalid_exit_price_fails_safely_without_touching_metadata(self):
        db = _db()
        rt = next(_make_runtime(db))
        _submit(rt)
        trade_id = db.list_trades()[0]["id"]
        # Invalid marks must never produce an exit (paper refuses px<=0)
        assert rt.on_option_quote("NSE_FO|69780", 0.0) is None
        assert rt.on_option_quote("NSE_FO|69780", -50.0) is None
        row = db.get_trade(trade_id)
        assert row["status"] == "filled"  # still open
        assert row["exit_price"] is None
        assert row["strike_price"] == 24800.0
        db.close()

    def test_missing_strike_in_signal_is_recorded_as_null_not_invented(self):
        db = _db()
        rt = next(_make_runtime(db))
        # Strike is not gated: an entry without it proceeds but the row
        # records NULL (renders N/A) — never a guessed value. (Expiry/lot
        # size ARE validated by the pre-trade contract gate — see
        # test_missing_lot_size_fails_safely_before_execution.)
        res = _submit(rt, dict(_CONTRACT, strike=None))
        assert res.accepted, getattr(res, "reason", None)
        row = db.list_trades()[0]
        assert row["strike_price"] is None
        assert row["capital_used"] == 9000.0  # capital does not depend on it
        assert historical_display_value(row["strike_price"]) == HISTORICAL_NOTE
        db.close()

    def test_missing_lot_size_fails_safely_before_execution(self):
        """Lot size is authoritative contract metadata: the paper risk gate
        REFUSES the entry rather than executing with an invented lot."""
        db = _db()
        rt = next(_make_runtime(db))
        res = _submit(rt, dict(_CONTRACT, lot_size=None))
        assert not res.accepted
        assert "LOT" in str(getattr(res, "reason", "")).upper()
        assert db.list_trades() == []
        db.close()

    def test_invalid_capital_inputs_leave_capital_null(self):
        m = normalize_trade_metadata({"entry_price": 120.0, "quantity": 75, "capital_used": 999999.0})
        assert m["capital_used"] == 9000.0, "a wrong stored capital must be recomputed from executed fill data"
        m2 = normalize_trade_metadata({"entry_price": None, "quantity": 75, "capital_used": 999999.0})
        assert m2["capital_used"] is None, "no valid fill data -> capital stays NULL"

    def test_list_trades_backward_compatible_row_shape(self):
        """Existing consumers keep working: keys are a superset of the old
        Trade dataclass field names."""
        db = _db()
        db.insert_trade(Trade(
            id="bc-1", symbol="NIFTY50", side="BUY", quantity=75, price=120.0,
            timestamp=datetime.now(timezone.utc), strategy="V8_D_PULLBACK_ATM",
            status="filled",
        ))
        row = db.list_trades()[0]
        for legacy_key in ("id", "symbol", "side", "quantity", "price", "timestamp",
                           "strategy", "status", "pnl", "notes"):
            assert legacy_key in row, f"backward compat broken: {legacy_key}"
        db.close()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
