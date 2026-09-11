"""Regression tests for the forensic-audit fix (trade accounting /
Overview dashboard showing 0 trades / ₹0 P&L despite real paper trades
having executed).

Root causes fixed:
  1. `insert_trade()` only ever wrote the legacy trades columns — the
     extended forensic columns (entry_price, exit_price, gross_pnl,
     net_pnl, indicators-at-entry, ...) were NEVER populated by any code
     path, so `_get_today_stats()` (which reads net_pnl/pnl) always
     computed 0 trades / ₹0 regardless of real trading activity.
  2. `backend/api/routers/overview.py` read its own module-level
     `RiskManager` instance instead of the live engine's — so the
     dashboard's Risk Meter could never reflect real trade/loss counts.

Follows this repo's test convention (see test_config.py, test_copilot.py):
no pytest fixtures, unittest.mock.patch.dict for env vars.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest import mock
from unittest.mock import MagicMock

from backend.database.db_manager import DatabaseManager
from backend.database.models import Trade


def _memory_db() -> DatabaseManager:
    db = DatabaseManager(":memory:")
    db.init_db()
    return db


class TestTradeEntryExitPersistence:
    """Direct db_manager round-trip — no TradingEngine involved, isolates
    the accounting layer itself."""

    def _insert_sample_trade(self, db: DatabaseManager, trade_id: str = "t1") -> None:
        trade = Trade(
            id=trade_id, symbol="NIFTY50", side="long", quantity=75,
            price=120.5, timestamp=datetime.now(timezone.utc),
            strategy="OPTION_PREMIUM", status="filled", pnl=None,
            notes="test",
        )
        db.insert_trade(trade)

    def test_insert_trade_leaves_extended_fields_null_until_filled_in(self):
        """Documents the ORIGINAL (correct, expected) behavior of
        insert_trade() — it should NOT itself populate extended fields;
        that's what record_trade_entry_details()/update_trade_exit() are
        for. A regression here would mean insert_trade silently grew
        scope it shouldn't have."""
        db = _memory_db()
        self._insert_sample_trade(db)
        row = db.get_trade("t1")
        assert row["entry_price"] is None
        assert row["exit_price"] is None
        assert row["net_pnl"] is None

    def test_record_trade_entry_details_populates_entry_fields(self):
        db = _memory_db()
        self._insert_sample_trade(db)
        db.record_trade_entry_details(
            trade_id="t1", entry_time=datetime.now(timezone.utc).isoformat(),
            entry_price=120.5, initial_stop=110.0, atr_at_entry=5.2,
            rsi_at_entry=62.3, choppiness_at_entry=45.0, volume_ratio=1.4,
            ema20_at_entry=22100.0, ema50_at_entry=22050.0, trend_bias="BULLISH",
        )
        row = db.get_trade("t1")
        assert row["entry_price"] == 120.5
        assert row["initial_stop"] == 110.0
        assert row["rsi_at_entry"] == 62.3
        assert row["trend_bias"] == "BULLISH"
        assert row["entry_time"] is not None

    def test_update_trade_exit_populates_exit_fields_and_legacy_pnl(self):
        db = _memory_db()
        self._insert_sample_trade(db)
        db.update_trade_exit(
            trade_id="t1", exit_time=datetime.now(timezone.utc).isoformat(),
            exit_price=100.0, exit_reason="STOP_LOSS", gross_pnl=-1537.5,
            net_pnl=-1550.0, brokerage=8.0, stt=4.5, pnl_r=-1.0,
            trade_duration_min=23,
        )
        row = db.get_trade("t1")
        assert row["exit_price"] == 100.0
        assert row["net_pnl"] == -1550.0
        assert row["exit_reason"] == "STOP_LOSS"
        # Legacy `pnl`/`status` columns (read by _get_today_stats/older
        # code) must ALSO be updated — this is what makes the dashboard
        # fix work without needing to touch overview.py's aggregation query.
        assert row["pnl"] == -1550.0
        assert row["status"] == "closed"

    def test_full_entry_to_exit_round_trip_matches_forensic_csv_shape(self):
        """The exact shape of the bug report: a trade that goes through
        insert -> entry details -> exit should end with EVERY forensic
        column populated, not None."""
        db = _memory_db()
        self._insert_sample_trade(db, "t2")
        db.record_trade_entry_details(
            trade_id="t2", entry_time="2026-09-10T09:20:00+00:00",
            entry_price=120.5, initial_stop=110.0, atr_at_entry=5.0,
            rsi_at_entry=55.0, choppiness_at_entry=40.0, volume_ratio=1.1,
            ema20_at_entry=22100.0, ema50_at_entry=22080.0, trend_bias="BULLISH",
            conditions_checked=json.dumps({"trend_aligned": True}),
        )
        db.update_trade_exit(
            trade_id="t2", exit_time="2026-09-10T09:43:00+00:00",
            exit_price=98.0, exit_reason="STOP_LOSS", gross_pnl=-1687.5,
            net_pnl=-1700.0, brokerage=8.2, stt=4.4, pnl_r=-1.0,
            trade_duration_min=23, final_stop=110.0,
        )
        row = db.get_trade("t2")
        for col in ("entry_time", "entry_price", "exit_time", "exit_price",
                    "gross_pnl", "net_pnl", "pnl_r", "exit_reason", "rsi_at_entry"):
            assert row[col] is not None, f"{col} should be populated after full entry+exit, was None"


class TestTodayStatsAggregation:
    """Reproduces backend/api/routers/overview.py's exact aggregation
    logic (duplicated here deliberately, to test the CONTRACT — that a
    fully-persisted trade is counted — without importing the router
    module's FastAPI machinery)."""

    def _today_stats(self, rows):
        wins = losses = 0
        pnl_total = 0.0
        for d in rows:
            pnl = float(d.get("net_pnl") or d.get("pnl") or 0)
            pnl_total += pnl
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
        total = wins + losses
        return {"total_trades": total, "wins": wins, "losses": losses,
                "win_rate": round(wins / total * 100, 1) if total else 0.0,
                "net_pnl": round(pnl_total, 2)}

    def test_unpersisted_exit_shows_zero_trades_reproducing_the_bug(self):
        """Before the fix: a trade with net_pnl=None and pnl=None (the
        exact CSV pattern from the bug report) contributes 0 to both
        wins and losses — this is the reported symptom, reproduced."""
        rows = [{"id": "t1", "net_pnl": None, "pnl": None}] * 6
        stats = self._today_stats(rows)
        assert stats["total_trades"] == 0
        assert stats["net_pnl"] == 0.0

    def test_persisted_exit_is_counted_as_a_loss(self):
        rows = [{"id": "t1", "net_pnl": -1550.0, "pnl": -1550.0}]
        stats = self._today_stats(rows)
        assert stats["total_trades"] == 1
        assert stats["losses"] == 1
        assert stats["net_pnl"] == -1550.0

    def test_six_real_losses_now_aggregate_correctly(self):
        """The exact scenario from the bug report — 6 real losing
        trades — now produces a non-zero total/loss count once exit data
        is actually persisted (post-fix), rather than the reported
        '0 trades, ₹0.00' regardless of real activity."""
        rows = [{"id": f"t{i}", "net_pnl": -500.0 * (i + 1), "pnl": -500.0 * (i + 1)} for i in range(6)]
        stats = self._today_stats(rows)
        assert stats["total_trades"] == 6
        assert stats["losses"] == 6
        assert stats["wins"] == 0
        assert stats["win_rate"] == 0.0
        assert stats["net_pnl"] == sum(-500.0 * (i + 1) for i in range(6))


class TestCloseFillsExitPersistence:
    """Integration test through the REAL TradingEngine._close_position()
    — verifies the actual production code path, not just db_manager in
    isolation."""

    def _engine_with_open_position(self):
        from backend.strategy.trading_engine import TradingEngine
        db = _memory_db()
        client = MagicMock()
        engine = TradingEngine(client=client, db_manager=db)

        trade_id = "trade-close-test"
        trade = Trade(
            id=trade_id, symbol="NIFTY50", side="long", quantity=75,
            price=120.5, timestamp=datetime.now(timezone.utc),
            strategy="OPTION_PREMIUM", status="filled", pnl=None, notes="",
        )
        db.insert_trade(trade)
        db.record_trade_entry_details(
            trade_id=trade_id, entry_time=(datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
            entry_price=120.5, initial_stop=110.0, rsi_at_entry=58.0,
        )
        engine._open_positions["NIFTY50"] = {
            "trade_id": trade_id, "entry_price": 120.5, "stop_loss": 110.0,
            "target": 140.0, "trailing_stop": 110.0, "strategy_name": "OPTION_PREMIUM",
            "quantity": 75, "requested_quantity": 75, "atr": 5.0, "side": "long",
            "entry_time": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
            "contract_instrument_key": "NSE_FO|TEST", "contract_info": {"option_type": "CE", "strike": 22000, "lot_size": 75},
            "expiry_date": "2026-09-25",
        }

        order = MagicMock()
        order.filled_quantity = 75
        order.quantity = 75
        order.average_fill_price = 100.0
        order.price = 100.0
        engine.order_manager.place_order = MagicMock(return_value=order)
        return engine, db, trade_id

    def test_close_position_persists_exit_data(self):
        import asyncio
        engine, db, trade_id = self._engine_with_open_position()
        asyncio.run(engine._close_position("NIFTY50", "STOP_LOSS"))

        row = db.get_trade(trade_id)
        assert row is not None
        assert row["exit_price"] == 100.0
        assert row["exit_reason"] == "STOP_LOSS"
        assert row["net_pnl"] is not None
        assert row["net_pnl"] < 0  # exit below entry -> a real loss, correctly persisted
        assert row["status"] == "closed"
        assert row["trade_duration_min"] is not None and row["trade_duration_min"] > 0

    def test_close_position_does_not_call_live_order_path(self):
        """Re-confirms this fix didn't touch broker-order safety — the
        mocked order_manager.place_order is the paper-mode-aware entry
        point already used everywhere else; this test just checks the
        exit flow still routes through it exactly once, not twice, and
        does nothing extra."""
        import asyncio
        engine, db, trade_id = self._engine_with_open_position()
        asyncio.run(engine._close_position("NIFTY50", "TARGET_HIT"))
        assert engine.order_manager.place_order.call_count == 1


class TestOverviewUsesLiveRiskManager:
    def test_overview_reads_engine_risk_manager_when_attached(self):
        """ROOT CAUSE FIX: overview.py used to construct its own
        module-level RiskManager and read THAT unconditionally — a
        phantom instance that never received a single
        record_trade_result() call from real trading. Now it must read
        request.app.state.engine.risk_manager when an engine is attached."""
        import asyncio
        from fastapi import FastAPI
        from starlette.requests import Request as StarletteRequest
        import backend.api.routers.overview as overview_module

        fake_engine = MagicMock()
        fake_engine.risk_manager.get_status.return_value = {"trades_today": 3, "consecutive_losses": 2, "source": "REAL_ENGINE"}

        app = FastAPI()
        app.state.engine = fake_engine

        async def _run():
            scope = {"type": "http", "app": app, "headers": [], "method": "GET", "path": "/overview"}
            request = StarletteRequest(scope)
            return await overview_module.get_overview(request)

        result = asyncio.run(_run())
        assert result["risk_status"]["source"] == "REAL_ENGINE"
        fake_engine.risk_manager.get_status.assert_called_once()

    def test_overview_falls_back_when_no_engine_attached(self):
        import asyncio
        from fastapi import FastAPI
        from starlette.requests import Request as StarletteRequest
        import backend.api.routers.overview as overview_module

        app = FastAPI()  # no .state.engine set

        async def _run():
            scope = {"type": "http", "app": app, "headers": [], "method": "GET", "path": "/overview"}
            request = StarletteRequest(scope)
            return await overview_module.get_overview(request)

        result = asyncio.run(_run())
        # Falls back to the local phantom instance rather than raising —
        # still functions, just can't reflect real state without an engine.
        assert "risk_status" in result
