import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from backend.database.db_manager import DatabaseManager
from backend.paper.paper_broker import PaperBroker


def make_runtime(db, capital="30000"):
    env = {
        "TRADING_MODE": "paper",
        "TRADING_STRATEGY": "V8_D_PULLBACK_ATM",
        "UPSTOX_ORDER_PRODUCT": "I",
        "TRADING_CAPITAL": capital,
        "RISK_PER_TRADE_PCT": "0.025",
        "MAX_DAILY_LOSS_PCT": "0.02",
        "MAX_TRADES_PER_DAY": "3",
        "MAX_CONCURRENT_POSITIONS": "1",
        "EOD_SQUARE_OFF": "15:15",
        "TRADING_BOT_OFFLINE_TESTS": "1",
    }
    with mock.patch.dict(os.environ, env):
        from backend.config.runtime_config import (
            invalidate_runtime_config_cache,
            set_runtime_config_db,
        )
        invalidate_runtime_config_cache()
        set_runtime_config_db(db)
        try:
            from backend.paper.paper_runtime import PaperTradingRuntime
            return PaperTradingRuntime(db=db, broker=PaperBroker())
        finally:
            set_runtime_config_db(None)
            invalidate_runtime_config_cache()


def test_legacy_empty_snapshot_migrates_to_configured_capital():
    with tempfile.TemporaryDirectory() as tmp:
        db = DatabaseManager(str(Path(tmp) / "test.db"))
        db.init_db()
        db.save_setting("paper_equity_snapshot", json.dumps({
            "realized_equity": 20000,
            "realized_pnl_total": 0,
            "charges_total": 0,
            "trade_day": "2026-10-23",
            "trades_today": 0,
            "daily_realized_pnl": 0,
        }))

        rt = make_runtime(db)
        assert rt.starting_capital == 30000
        assert rt.realized_equity == 30000

        rt._persist_day_state()
        saved = json.loads(db.get_setting("paper_equity_snapshot"))
        assert saved["starting_capital"] == 30000
        assert saved["realized_equity"] == 30000
        db.close()


def test_known_baseline_preserves_equity_change_on_capital_change():
    with tempfile.TemporaryDirectory() as tmp:
        db = DatabaseManager(str(Path(tmp) / "test.db"))
        db.init_db()
        db.save_setting("paper_equity_snapshot", json.dumps({
            "starting_capital": 20000,
            "realized_equity": 19500,
            "realized_pnl_total": -500,
            "charges_total": 0,
            "trade_day": "2026-10-23",
            "trades_today": 0,
            "daily_realized_pnl": 0,
        }))

        rt = make_runtime(db, "30000")
        assert rt.starting_capital == 30000
        assert rt.realized_equity == 29500
        db.close()
