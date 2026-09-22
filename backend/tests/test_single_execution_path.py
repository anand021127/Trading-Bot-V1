"""Integration tests: single controlled execution path for Paper."""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock
from unittest.mock import MagicMock

# fastapi may be unavailable in the test image — stub so bot_control imports.
class _NoopRouter:
    def get(self, *a, **k):
        def deco(fn):
            return fn
        return deco

    post = get


class _FastapiStub:
    def APIRouter(self, *a, **k):
        return _NoopRouter()


if "fastapi" not in sys.modules:
    sys.modules["fastapi"] = _FastapiStub()  # type: ignore

import importlib.util
_bc_path = os.path.join(os.path.dirname(__file__), "..", "api", "routers", "bot_control.py")
_spec = importlib.util.spec_from_file_location("bot_control_mod", os.path.abspath(_bc_path))
bot_control = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(bot_control)

from backend.database.db_manager import DatabaseManager
from backend.execution.kill_switch import FULL_SYSTEM_STOP, PersistentKillSwitch
from backend.orders.order_manager import OrderManager
from backend.orders.order_models import Order, OrderStatus
from backend.paper.paper_runtime import PaperTradingRuntime
from backend.strategy.signal import SignalType, StrategySignal
from backend.strategy.trading_engine import BotState, TradingEngine


def _arm_settings():
    import backend.strategy.trading_engine as te_mod
    te_mod.settings.mode = "paper"
    te_mod.settings.strategy.name = "V8_D_PULLBACK_ATM"
    te_mod.settings.order.product = "I"
    te_mod.settings.risk.max_risk_per_trade_pct = 0.025
    te_mod.settings.capital.total = 100000.0
    te_mod.settings.capital.max_allocation_per_trade = 0.18


def _engine(db_path: str) -> TradingEngine:
    _arm_settings()
    db = DatabaseManager(db_path=db_path)
    db.init_db()
    client = MagicMock()
    om = OrderManager(client=client, paper_mode=True, default_product="I")
    eng = TradingEngine(db_manager=db, client=client, order_manager=om)
    eng._init_execution_pipeline()
    eng._position_mismatch = False
    eng._reconciled = True
    return eng


def _signal():
    sig = StrategySignal(
        strategy_name="V8_D_PULLBACK_ATM",
        symbol="NIFTY50",
        signal=SignalType.BUY,
        confidence=80.0,
        entry_price=80.0,
        stop_loss=57.6,
        target=113.6,
        generated_at="2026-09-20T10:00:00+05:30",
    )
    sig.indicators = {
        "selected_contract": {
            "option_type": "CE",
            "strike": 24000,
            "instrument_key": "NSE_FO|99999",
            "lot_size": 75,
            "freeze_quantity": 1800,
            "expiry": "2027-01-07",
        },
        "expiry_date": "2027-01-07",
        "spot_price": 24010.0,
        "quote_age_seconds": 1,
        "atr": 6.0,
        "option_atr": 6.0,
    }
    return sig


def _allow_risk(eng: TradingEngine):
    eng.risk_manager.check_lot_risk = lambda **kw: (True, "ok")
    eng.risk_manager.can_take_trade = lambda *a, **k: (True, "ok")
    eng.risk_manager.check_exposure = lambda **kw: (True, "ok")


def test_engine_entry_goes_through_pipeline_not_direct_place():
    path = os.path.join(tempfile.mkdtemp(), "eng.db")
    eng = _engine(path)
    _allow_risk(eng)
    assert eng._pipeline is not None
    called = {"n": 0}
    real_place = eng.order_manager.place_order

    def wrap(req):
        called["n"] += 1
        return real_place(req)

    eng.order_manager.place_order = wrap
    orig_submit = eng._pipeline.submit_signal
    saw_pipeline = {"yes": False}

    def wrap_submit(sig):
        saw_pipeline["yes"] = True
        return orig_submit(sig)

    eng._pipeline.submit_signal = wrap_submit
    with mock.patch.object(eng.position_sizer, "calculate", return_value=75):
        trade_id = eng.execute_multi_signal(_signal())
    assert saw_pipeline["yes"] is True
    assert trade_id is not None
    assert called["n"] == 1  # only via pipeline callback


def test_engine_refuses_without_pipeline():
    path = os.path.join(tempfile.mkdtemp(), "eng2.db")
    eng = _engine(path)
    _allow_risk(eng)
    eng._pipeline = None
    with mock.patch.object(eng.position_sizer, "calculate", return_value=75):
        trade_id = eng.execute_multi_signal(_signal())
    assert trade_id is None


def test_duplicate_signal_through_engine():
    path = os.path.join(tempfile.mkdtemp(), "eng3.db")
    eng = _engine(path)
    _allow_risk(eng)
    with mock.patch.object(eng.position_sizer, "calculate", return_value=75):
        t1 = eng.execute_multi_signal(_signal())
        t2 = eng.execute_multi_signal(_signal())
    assert t1 is not None
    assert t2 is None


def test_paper_start_does_not_start_engine_loop():
    import asyncio
    bot_control.settings.mode = "paper"
    BotState.stop("test")
    BotState.reset_kill()
    rt = MagicMock()
    bot_control.set_paper_runtime(rt)
    bot_control.set_engine(MagicMock())
    result = asyncio.run(bot_control.start_bot())
    assert result["success"] is True
    assert result.get("executor") == "PaperTradingRuntime"
    BotState.stop("test cleanup")


def test_paper_start_fails_without_runtime():
    import asyncio
    import tempfile
    from backend.database.db_manager import DatabaseManager

    path = os.path.join(tempfile.mkdtemp(), "botstate.db")
    BotState._db = DatabaseManager(db_path=path)
    bot_control.settings.mode = "paper"
    BotState.stop("test")
    BotState.reset_kill()
    bot_control.set_paper_runtime(None)
    try:
        import backend.api.main as main_mod
        if getattr(getattr(main_mod, "app", None), "state", None) is not None:
            main_mod.app.state.paper_runtime = None
    except Exception:
        pass
    result = asyncio.run(bot_control.start_bot())
    assert result["success"] is False
    assert "PaperTradingRuntime" in result["message"]
    BotState._db = None


def test_live_start_blocked():
    import asyncio
    bot_control.settings.mode = "live"
    BotState.stop("test")
    BotState.reset_kill()
    result = asyncio.run(bot_control.start_bot())
    assert result["success"] is False
    assert "not enabled" in result["message"].lower()


def test_shared_kill_switch_two_runtimes():
    path = os.path.join(tempfile.mkdtemp(), "shared.db")
    env = {
        "TRADING_MODE": "paper",
        "TRADING_STRATEGY": "V8_D_PULLBACK_ATM",
        "UPSTOX_ORDER_PRODUCT": "I",
        "DATABASE_PATH": path,
        "RISK_PER_TRADE_PCT": "0.025",
    }
    morning = datetime(2026, 9, 20, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    with mock.patch.dict(os.environ, env, clear=False):
        rt1 = PaperTradingRuntime()
        rt1.now_fn = lambda: morning
        rt1.kill.set_level(FULL_SYSTEM_STOP, "test")
        rt2 = PaperTradingRuntime()
        rt2.now_fn = lambda: morning
    assert rt2.kill.level() == FULL_SYSTEM_STOP
    blocked = rt2.submit_entry({
        "timestamp": "2026-09-20T10:00:00+05:30",
        "instrument_key": "NSE_FO|1",
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
        "quote_age_seconds": 1,
    })
    assert blocked.accepted is False


def test_paper_mode_order_manager_is_paper():
    _arm_settings()
    path = os.path.join(tempfile.mkdtemp(), "pm.db")
    eng = _engine(path)
    assert eng.order_manager.paper_mode is True


def test_no_direct_engine_place_order_in_source():
    """Static guard: execute_multi_signal body must not call place_order directly."""
    src = open("backend/strategy/trading_engine.py", encoding="utf-8").read()
    # After the pipeline helper's place callback, the only place_order should be inside _place
    assert "order = self.order_manager.place_order(req)" not in src
    assert "_submit_entry_via_pipeline" in src
    assert "_submit_exit_via_pipeline" in src
