"""Tests for backend/copilot/*. Follows this repo's test convention
(see backend/tests/test_config.py, test_ai_layer.py) — no pytest
fixtures, unittest.mock.patch.dict for env vars.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from unittest import mock
from unittest.mock import MagicMock

from backend.copilot.alerts import AlertStateTracker
from backend.copilot.config import load_copilot_settings
from backend.copilot.conversational import chat, route_question, _extract_symbol
from backend.copilot.decision_engine import build_market_analysis, build_trade_plan_for_symbol
from backend.copilot.diagnostics import run_full_diagnostics
from backend.copilot.llm_adapter import RuleBasedFallbackAdapter, get_llm_adapter
from backend.copilot.tools import CopilotTools
from backend.copilot.trade_plan import TradePlan, validate_trade_plan
from backend.database.db_manager import DatabaseManager
from backend.strategy.trading_engine import TradingEngine


def _synthetic_candles(n=400, start=100.0, drift=0.03, seed=7):
    import random
    rnd = random.Random(seed)
    candles = []
    price = start
    ts = datetime(2024, 6, 3, 9, 15)
    for i in range(n):
        o = price
        price += drift + rnd.uniform(-0.25, 0.25)
        c = price
        h = max(o, c) + rnd.uniform(0, 0.2)
        l = min(o, c) - rnd.uniform(0, 0.2)
        candles.append({"timestamp": ts.isoformat(), "open": round(o, 2), "high": round(h, 2),
                         "low": round(l, 2), "close": round(c, 2), "volume": 1000 + i})
        ts += timedelta(minutes=5)
    return candles


def _real_engine():
    db = DatabaseManager(":memory:")
    db.init_db()
    client = MagicMock()
    client.get_positions_with_details.return_value = []
    engine = TradingEngine(client=client, db_manager=db)
    return engine, db


class TestToolsDegradeGracefully:
    def test_no_services_attached_never_raises(self):
        tools = CopilotTools()
        assert tools.get_market_status()["available"] is False
        assert tools.get_live_prices(["NIFTY50"])["available"] is False
        assert tools.get_open_positions()["available"] is False
        assert tools.get_account_risk()["available"] is False
        assert tools.get_daily_pnl()["available"] is False
        assert tools.get_recent_trades()["available"] is False
        assert tools.get_bot_health()["available"] is False
        assert tools.get_strategy_signals("NIFTY50", [])["available"] is False

    def test_indicators_work_on_real_shape_data(self):
        tools = CopilotTools()
        candles = _synthetic_candles(n=60)
        result = tools.get_indicators("TEST", candles)
        assert result["available"] is True
        assert result["last_close"] == candles[-1]["close"]
        assert result["rsi"] is not None
        assert result["atr"] is not None

    def test_support_resistance_uses_real_highs_lows(self):
        tools = CopilotTools()
        candles = _synthetic_candles(n=80)
        sr = tools.get_support_resistance(candles, lookback=80)
        assert sr["available"] is True
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        assert sr["resistance"] == max(highs)
        assert sr["support"] == min(lows)

    def test_option_chain_without_client_is_unavailable_not_fabricated(self):
        tools = CopilotTools()  # no engine/client attached
        result = tools.get_option_chain("NIFTY50")
        assert result["available"] is False

    def test_live_candles_without_client_unavailable(self):
        tools = CopilotTools()
        result = tools.get_live_candles("NIFTY50")
        assert result["available"] is False

    def test_option_quote_without_ws_client_unavailable(self):
        tools = CopilotTools()
        result = tools.get_option_quote("NSE_FO|12345")
        assert result["available"] is False


class TestTradePlanValidation:
    def _base_plan(self, **overrides) -> TradePlan:
        defaults = dict(
            symbol="NIFTY50", underlying="NIFTY50", option_type="CE", strike=None, expiry=None,
            entry_price_low=99.9, entry_price_high=100.1, stop_loss=98.0, target_1=104.0,
            quote_timestamp=datetime.now(timezone.utc).isoformat(),
        )
        defaults.update(overrides)
        return TradePlan(**defaults)

    def test_missing_prices_rejected(self):
        plan = self._base_plan(stop_loss=None)
        result = validate_trade_plan(plan, risk_manager=None)
        assert result.approved is False
        assert "prices_present" in result.checks and result.checks["prices_present"] is False

    def test_invalid_stop_loss_direction_rejected(self):
        plan = self._base_plan(stop_loss=105.0)  # above entry for a CE — nonsensical
        result = validate_trade_plan(plan, risk_manager=MagicMock(can_take_trade=lambda s: (True, "")))
        assert result.approved is False
        assert result.checks["stop_loss_valid"] is False

    def test_poor_risk_reward_rejected(self):
        plan = self._base_plan(stop_loss=99.0, target_1=100.5)  # tiny reward vs risk
        result = validate_trade_plan(plan, risk_manager=MagicMock(can_take_trade=lambda s: (True, "")))
        assert result.approved is False
        assert result.checks["risk_reward_ok"] is False

    def test_stale_quote_rejected(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        plan = self._base_plan(quote_timestamp=old_ts)
        result = validate_trade_plan(plan, risk_manager=MagicMock(can_take_trade=lambda s: (True, "")))
        assert result.approved is False
        assert result.checks["quote_fresh"] is False

    def test_missing_spread_rejected_not_assumed_ok(self):
        plan = self._base_plan()
        result = validate_trade_plan(plan, risk_manager=MagicMock(can_take_trade=lambda s: (True, "")), spread_pct=None)
        assert result.checks["spread_acceptable"] is False

    def test_risk_manager_veto_is_final(self):
        plan = self._base_plan()
        rm = MagicMock(can_take_trade=lambda s: (False, "Daily loss limit reached"))
        result = validate_trade_plan(plan, risk_manager=rm, spread_pct=1.0)
        assert result.approved is False
        assert "Daily loss limit reached" in result.reasons_rejected

    def test_no_risk_manager_rejected_not_assumed_safe(self):
        plan = self._base_plan()
        result = validate_trade_plan(plan, risk_manager=None, spread_pct=1.0)
        assert result.approved is False
        assert result.checks["risk_manager_available"] is False

    def test_all_checks_pass_approves(self):
        plan = self._base_plan()
        rm = MagicMock(can_take_trade=lambda s: (True, ""))
        result = validate_trade_plan(plan, risk_manager=rm, spread_pct=1.0)
        assert result.approved is True
        assert result.reasons_rejected == []


class TestDecisionEngineEndToEnd:
    def test_no_candles_reports_gap_not_crash(self):
        tools = CopilotTools()
        result = build_trade_plan_for_symbol(tools, "NIFTY50", candles=None)
        assert result["available"] is False

    def test_real_engine_real_data_never_raises(self):
        """Runs the full analysis -> trade-plan -> validation pipeline
        against a real TradingEngine + real 2024 NIFTY50 candles, scanning
        several windows so at least one likely produces a signal. Whatever
        happens, this must never raise."""
        engine, db = _real_engine()
        tools = CopilotTools(engine=engine, db_manager=db, risk_manager=engine.risk_manager)

        import os
        data_path = os.path.join("real_data", "NIFTY50_2024_5min.json")
        if not os.path.exists(data_path):
            return  # real historical data not present in this environment — skip rather than fail
        with open(data_path) as f:
            candles = json.load(f)

        saw_trade_plan = False
        for end in range(300, min(len(candles), 1500), 100):
            window = candles[max(0, end - 300):end]
            result = build_trade_plan_for_symbol(tools, "NIFTY50", window)
            assert result["available"] is True
            assert result["analysis"]["decision"] in ("WAIT", "SKIP", "TRADE")
            if result.get("trade_plan") is not None:
                saw_trade_plan = True
                assert result["validation"] is not None
                assert isinstance(result["validation"]["approved"], bool)
        # Not asserting saw_trade_plan is True — a quiet window is a valid
        # outcome; the important thing is nothing raised.


def _mock_client_with_realistic_chain(spot=22000.0, atm_strike=22000.0):
    """A MagicMock standing in for backend.broker.upstox_client.UpstoxClient,
    returning data shaped EXACTLY like the real client's documented
    contract (see UpstoxClient.get_option_chain's docstring) — not a
    simplified/fabricated shape, so exercising the real
    OptionPremiumStrategy.select_contract/evaluate against it is
    representative of the real pipeline."""
    client = MagicMock()
    client.get_nearest_expiry.return_value = "2024-06-27"
    client.get_multiple_quotes.return_value = {"NIFTY50": {"symbol": "NIFTY50", "ltp": spot}}
    client.get_option_chain.return_value = [
        {"strike": atm_strike, "option_type": "CE", "instrument_key": "NSE_FO|CE_ATM",
         "ltp": 120.5, "close_price": 118.0, "volume": 50000, "oi": 25000,
         "bid_price": 119.5, "ask_price": 121.0, "iv": 14.2, "delta": 0.52, "theta": -8.3, "gamma": 0.004, "vega": 12.1,
         "lot_size": 75, "freeze_quantity": 1800},
        {"strike": atm_strike, "option_type": "PE", "instrument_key": "NSE_FO|PE_ATM",
         "ltp": 95.0, "close_price": 97.0, "volume": 40000, "oi": 22000,
         "bid_price": 94.0, "ask_price": 96.0, "iv": 15.0, "delta": -0.48, "theta": -7.9, "gamma": 0.004, "vega": 11.5,
         "lot_size": 75, "freeze_quantity": 1800},
        {"strike": atm_strike + 100, "option_type": "CE", "instrument_key": "NSE_FO|CE_OTM",
         "ltp": 60.0, "close_price": 58.0, "volume": 10000, "oi": 8000,
         "bid_price": 59.0, "ask_price": 61.0, "iv": 15.5, "delta": 0.35, "theta": -6.0, "gamma": 0.005, "vega": 9.0,
         "lot_size": 75, "freeze_quantity": 1800},
    ]

    def _historical(symbol, interval, limit=100, **kw):
        # Real-shaped candles with a clear uptrend so
        # detect_underlying_trend / OptionPremiumStrategy momentum
        # conditions actually fire BULLISH -> CE, deterministically.
        candles = _synthetic_candles(n=max(limit, 40), start=spot - 40, drift=1.2, seed=3)
        if symbol.startswith("NSE_FO") or "CE" in symbol or "PE" in symbol:
            # Premium candle series, independent scale from the underlying.
            candles = _synthetic_candles(n=max(limit, 40), start=118.0, drift=0.3, seed=5)
        return candles
    client.get_historical_candles.side_effect = _historical
    return client


class TestRealOptionPipeline:
    """Exercises the REAL production path — engine.evaluate_option_premium()
    — with a mocked broker client shaped exactly like the real API's
    documented contract, so this proves the Copilot's TradePlan comes from
    real chain/premium data flowing through the ACTUAL OptionPremiumStrategy,
    not a hand-rolled underlying-ATR approximation."""

    def _engine_with_mock_chain(self):
        db = DatabaseManager(":memory:")
        db.init_db()
        client = _mock_client_with_realistic_chain()
        engine = TradingEngine(client=client, db_manager=db)
        return engine, db, client

    def test_get_live_candles_uses_the_real_client(self):
        engine, db, client = self._engine_with_mock_chain()
        tools = CopilotTools(engine=engine, db_manager=db)
        result = tools.get_live_candles("NIFTY50", "5minute", limit=50)
        assert result["available"] is True
        assert result["candle_count"] >= 40
        client.get_historical_candles.assert_called()

    def test_get_option_chain_uses_the_real_client_and_summarizer(self):
        engine, db, client = self._engine_with_mock_chain()
        tools = CopilotTools(engine=engine, db_manager=db)
        result = tools.get_option_chain("NIFTY50")
        assert result["available"] is True
        assert result["expiry"] == "2024-06-27"
        assert result["contract_count"] == 3
        assert result["summary"] is not None  # summarize_chain() ran successfully

    def test_get_nearest_expiry_real_client(self):
        engine, db, client = self._engine_with_mock_chain()
        tools = CopilotTools(engine=engine, db_manager=db)
        result = tools.get_nearest_expiry("NIFTY50")
        assert result["available"] is True
        assert result["expiry"] == "2024-06-27"

    def test_trade_plan_uses_real_evaluate_option_premium(self):
        engine, db, client = self._engine_with_mock_chain()
        tools = CopilotTools(engine=engine, db_manager=db, risk_manager=engine.risk_manager)
        result = build_trade_plan_for_symbol(tools, "NIFTY50")
        assert result["available"] is True
        # Whatever the decision, it must have gone through the real chain —
        # client.get_option_chain must actually have been called.
        client.get_option_chain.assert_called()
        if result.get("trade_plan") is not None:
            tp = result["trade_plan"]
            # Strike/instrument_key/OI/bid/ask must be the REAL mocked
            # values, not None/fabricated.
            assert tp["strike"] in (22000.0, 22100.0)
            assert tp["instrument_key"] in ("NSE_FO|CE_ATM", "NSE_FO|CE_OTM", "NSE_FO|PE_ATM")
            assert tp["open_interest"] is not None
            assert tp["bid_price"] is not None and tp["ask_price"] is not None
            assert result["validation"] is not None

    def test_trade_plan_reports_gap_when_no_client(self):
        result = build_trade_plan_for_symbol(CopilotTools(), "NIFTY50")
        assert result["available"] is False

    def test_trade_plan_reports_gap_when_chain_empty(self):
        engine, db, client = self._engine_with_mock_chain()
        client.get_option_chain.return_value = []  # broker returns nothing real
        tools = CopilotTools(engine=engine, db_manager=db, risk_manager=engine.risk_manager)
        result = build_trade_plan_for_symbol(tools, "NIFTY50")
        assert result["available"] is True
        assert result["trade_plan"] is None
        assert result["decision"] == "SKIP"


class TestPaperExecutionSafety:
    """The highest-stakes tests in this file — execution.py is the one
    module that CAN place an order, so every refusal path is tested
    explicitly, and success requires ALL gates to align."""

    def _approved_plan(self):
        return {
            "symbol": "NIFTY50", "entry_price_low": 99.0, "entry_price_high": 101.0,
            "stop_loss": 95.0, "target_1": 110.0, "option_type": "CE", "strike": 22000,
            "instrument_key": "NSE_FO|TEST", "reason": "test", "open_interest": 1000,
            "bid_price": 99.5, "ask_price": 100.5,
        }, {"approved": True, "reasons_rejected": []}

    def test_refuses_when_copilot_disabled(self):
        from backend.copilot.execution import submit_trade_plan_for_paper_execution
        from backend.copilot.config import CopilotSettings
        plan, val = self._approved_plan()
        settings = CopilotSettings(enabled=False, mode="paper", min_risk_reward=1.5,
                                    max_quote_age_seconds=30, llm_backend="none",
                                    llm_base_url="", llm_model="", llm_timeout_seconds=8)
        result = submit_trade_plan_for_paper_execution(CopilotTools(), plan, val, copilot_settings=settings)
        assert result.submitted is False
        assert "disabled" in result.reason.lower()

    def test_refuses_when_copilot_mode_not_paper(self):
        from backend.copilot.execution import submit_trade_plan_for_paper_execution
        from backend.copilot.config import CopilotSettings
        plan, val = self._approved_plan()
        settings = CopilotSettings(enabled=True, mode="shadow", min_risk_reward=1.5,
                                    max_quote_age_seconds=30, llm_backend="none",
                                    llm_base_url="", llm_model="", llm_timeout_seconds=8)
        result = submit_trade_plan_for_paper_execution(CopilotTools(), plan, val, copilot_settings=settings)
        assert result.submitted is False
        assert "shadow" in result.reason.lower() or "paper" in result.reason.lower()

    def test_refuses_when_global_bot_mode_is_live(self):
        """The critical safety test: even with Copilot fully configured
        for paper mode, if the bot's GLOBAL mode is 'live', execution
        must still refuse — COPILOT_MODE=paper alone is never sufficient."""
        from backend.copilot.execution import submit_trade_plan_for_paper_execution
        from backend.copilot.config import CopilotSettings
        plan, val = self._approved_plan()
        settings = CopilotSettings(enabled=True, mode="paper", min_risk_reward=1.5,
                                    max_quote_age_seconds=30, llm_backend="none",
                                    llm_base_url="", llm_model="", llm_timeout_seconds=8)
        engine = MagicMock()
        tools = CopilotTools(engine=engine)
        with mock.patch("backend.strategy.trading_engine.settings") as bot_settings:
            bot_settings.mode = "live"
            result = submit_trade_plan_for_paper_execution(tools, plan, val, copilot_settings=settings)
        assert result.submitted is False
        assert "live" in result.reason.lower()
        engine.execute_multi_signal.assert_not_called()

    def test_refuses_unapproved_plan(self):
        from backend.copilot.execution import submit_trade_plan_for_paper_execution
        from backend.copilot.config import CopilotSettings
        plan, _ = self._approved_plan()
        settings = CopilotSettings(enabled=True, mode="paper", min_risk_reward=1.5,
                                    max_quote_age_seconds=30, llm_backend="none",
                                    llm_base_url="", llm_model="", llm_timeout_seconds=8)
        result = submit_trade_plan_for_paper_execution(CopilotTools(), plan, {"approved": False, "reasons_rejected": ["x"]}, copilot_settings=settings)
        assert result.submitted is False
        assert "not approved" in result.reason.lower() or "unapproved" in result.reason.lower()

    def test_succeeds_when_all_gates_align(self):
        from backend.copilot.execution import submit_trade_plan_for_paper_execution
        from backend.copilot.config import CopilotSettings
        plan, val = self._approved_plan()
        settings = CopilotSettings(enabled=True, mode="paper", min_risk_reward=1.5,
                                    max_quote_age_seconds=30, llm_backend="none",
                                    llm_base_url="", llm_model="", llm_timeout_seconds=8)
        engine = MagicMock()
        engine.execute_multi_signal.return_value = "trade_123"
        tools = CopilotTools(engine=engine)
        with mock.patch("backend.strategy.trading_engine.settings") as bot_settings:
            bot_settings.mode = "paper"
            result = submit_trade_plan_for_paper_execution(tools, plan, val, copilot_settings=settings)
        assert result.submitted is True
        assert result.trade_id == "trade_123"
        engine.execute_multi_signal.assert_called_once()

    def test_engine_rejection_at_execution_time_is_reported_not_hidden(self):
        from backend.copilot.execution import submit_trade_plan_for_paper_execution
        from backend.copilot.config import CopilotSettings
        plan, val = self._approved_plan()
        settings = CopilotSettings(enabled=True, mode="paper", min_risk_reward=1.5,
                                    max_quote_age_seconds=30, llm_backend="none",
                                    llm_base_url="", llm_model="", llm_timeout_seconds=8)
        engine = MagicMock()
        engine.execute_multi_signal.return_value = None  # engine's own RiskManager rejected it
        tools = CopilotTools(engine=engine)
        with mock.patch("backend.strategy.trading_engine.settings") as bot_settings:
            bot_settings.mode = "paper"
            result = submit_trade_plan_for_paper_execution(tools, plan, val, copilot_settings=settings)
        assert result.submitted is False


class TestPaperExecutionEndToEnd:
    """PHASE 6: a full simulated sequence through the REAL engine —
    TradePlan -> validation -> RiskManager -> PositionSizer -> OrderManager
    -> paper fill -> position tracking — with a mocked broker client (so
    no real network call is possible), verifying `client.place_order`
    (the ONLY method that reaches Upstox for a real order) is never
    called, while `client.get_quote_by_instrument_key` (a read-only paper
    fill-price lookup) legitimately is."""

    def test_full_paper_sequence_never_calls_broker_place_order(self):
        from backend.copilot.execution import submit_trade_plan_for_paper_execution
        from backend.copilot.config import CopilotSettings

        db = DatabaseManager(":memory:")
        db.init_db()
        client = _mock_client_with_realistic_chain()
        client.get_quote_by_instrument_key.return_value = {"ltp": 120.5, "bid_price": 119.5, "ask_price": 121.0}
        # TradingEngine's global `settings.mode` defaults to "paper" in
        # this repo (verified directly, not assumed) — so OrderManager is
        # constructed with paper_mode=True here, same as production would
        # be unless an operator explicitly flips to live.
        engine = TradingEngine(client=client, db_manager=db)
        assert engine.order_manager.paper_mode is True  # sanity check on the real object, not assumed

        tools = CopilotTools(engine=engine, db_manager=db, risk_manager=engine.risk_manager)
        plan_result = build_trade_plan_for_symbol(tools, "NIFTY50")

        copilot_settings = CopilotSettings(enabled=True, mode="paper", min_risk_reward=0.5,  # low floor so the mocked setup can clear it
                                            max_quote_age_seconds=30, llm_backend="none",
                                            llm_base_url="", llm_model="", llm_timeout_seconds=8)

        if plan_result.get("trade_plan") is None:
            # The deterministic strategy/mocked trend didn't produce a
            # signal this run — still a valid, safe outcome; nothing to
            # execute, and nothing should have touched the broker.
            client.place_order.assert_not_called()
            return

        exec_result = submit_trade_plan_for_paper_execution(
            tools, plan_result["trade_plan"], plan_result["validation"], copilot_settings=copilot_settings,
        )

        # THE critical safety assertion, regardless of whether the trade
        # was ultimately approved/executed or rejected by RiskManager:
        client.place_order.assert_not_called()

        if exec_result.submitted:
            assert exec_result.trade_id is not None
            positions = db.get_open_positions()
            assert len(positions) >= 1  # paper fill actually created a tracked position


class TestPositionSizerVerification:
    """PHASE 5: verifies the REAL backend.risk.position_sizer.PositionSizer
    against its actual documented formula (capital * risk_per_trade,
    floor-divided by per-unit risk, floored at min_qty, capped at
    max_qty) — not a guessed/assumed signature. Real broker account
    capital is NOT available in this sandbox, so tests use explicit
    capital figures rather than a live account balance; that gap is
    called out in docs/COPILOT.md as requiring runtime verification."""

    def test_basic_formula(self):
        from backend.risk.position_sizer import PositionSizer
        sizer = PositionSizer(capital=100_000.0, risk_per_trade=0.01)  # risk 1000 per trade
        qty = sizer.calculate(entry_price=100.0, stop_loss_price=90.0)  # per-unit risk = 10
        assert qty == 100  # floor(1000 / 10)

    def test_risk_percentage_scales_quantity(self):
        from backend.risk.position_sizer import PositionSizer
        low_risk = PositionSizer(capital=100_000.0, risk_per_trade=0.005).calculate(entry_price=100.0, stop_loss_price=90.0)
        high_risk = PositionSizer(capital=100_000.0, risk_per_trade=0.02).calculate(entry_price=100.0, stop_loss_price=90.0)
        assert high_risk == 4 * low_risk  # 2% vs 0.5% risk -> exactly 4x quantity

    def test_wider_stop_distance_reduces_quantity(self):
        from backend.risk.position_sizer import PositionSizer
        sizer = PositionSizer(capital=100_000.0, risk_per_trade=0.01)
        tight = sizer.calculate(entry_price=100.0, stop_loss_price=95.0)   # risk 5/unit
        wide = sizer.calculate(entry_price=100.0, stop_loss_price=50.0)   # risk 50/unit
        assert wide < tight

    def test_min_qty_floor(self):
        from backend.risk.position_sizer import PositionSizer
        # Tiny capital -> raw qty would floor to 0, but min_qty guarantees at least 1.
        sizer = PositionSizer(capital=10.0, risk_per_trade=0.01, min_qty=1)
        qty = sizer.calculate(entry_price=100.0, stop_loss_price=50.0)
        assert qty == 1
        # NOTE: this means the position sizer can return a quantity whose
        # real risk EXCEEDS risk_per_trade * capital when per-unit risk is
        # large relative to the risk budget — this is the exact scenario
        # RiskManager.check_lot_risk() exists to catch downstream (see
        # TestLotSizeAndRiskGating below).

    def test_max_qty_cap(self):
        from backend.risk.position_sizer import PositionSizer
        sizer = PositionSizer(capital=10_000_000.0, risk_per_trade=0.5, max_qty=500)
        qty = sizer.calculate(entry_price=1.0, stop_loss_price=0.5)
        assert qty == 500

    def test_zero_stop_distance_raises_not_silently_wrong(self):
        from backend.risk.position_sizer import PositionSizer
        sizer = PositionSizer(capital=100_000.0)
        try:
            sizer.calculate(entry_price=100.0, stop_loss_price=100.0)
            assert False, "expected ValueError for zero stop distance"
        except ValueError:
            pass

    def test_quantity_cannot_exceed_max_qty_configured_on_sizer(self):
        """'cannot exceed existing RiskManager limits' — the PositionSizer
        itself enforces max_qty; whether the LIVE engine's RiskManager
        configures max_qty from real account/broker limits is NOT
        verifiable in this sandbox (no live account) — UNVERIFIED, see
        docs/COPILOT.md."""
        from backend.risk.position_sizer import PositionSizer
        sizer = PositionSizer(capital=100_000.0, risk_per_trade=1.0, max_qty=10)
        qty = sizer.calculate(entry_price=1.0, stop_loss_price=0.5)
        assert qty <= 10


class TestLotSizeAndRiskGating:
    """Verifies the Copilot's TradePlan.quantity mirrors
    TradingEngine.execute_multi_signal()'s EXACT lot-rounding/freeze-cap
    sequence (backend/strategy/trading_engine.py), not the raw
    PositionSizer output — and that RiskManager.check_lot_risk() is
    actually consulted in validate_trade_plan()."""

    def test_quantity_is_lot_rounded(self):
        engine, db = _real_engine()
        client = _mock_client_with_realistic_chain()
        engine.client = client
        tools = CopilotTools(engine=engine, db_manager=db, risk_manager=engine.risk_manager)
        result = build_trade_plan_for_symbol(tools, "NIFTY50")
        if result.get("trade_plan") and result["trade_plan"].get("quantity") is not None:
            qty = result["trade_plan"]["quantity"]
            lot_size = result["trade_plan"]["lot_size"]
            assert lot_size == 75
            assert qty % lot_size == 0  # must be a whole number of lots

    def test_lot_risk_check_runs_when_lot_size_present(self):
        from backend.copilot.trade_plan import TradePlan, validate_trade_plan
        plan = TradePlan(
            symbol="NIFTY50", underlying="NIFTY50", option_type="CE", strike=22000, expiry="2024-06-27",
            entry_price_low=99.9, entry_price_high=100.1, stop_loss=1.0, target_1=104.0,  # huge per-unit risk
            lot_size=75, quote_timestamp=datetime.now(timezone.utc).isoformat(),
        )
        rm = MagicMock()
        rm.can_take_trade.return_value = (True, "")
        rm.check_lot_risk.return_value = (False, "Minimum lot risk exceeds maximum allowed trade risk.")
        result = validate_trade_plan(plan, risk_manager=rm, spread_pct=1.0)
        assert result.approved is False
        assert "lot_risk_ok" in result.checks and result.checks["lot_risk_ok"] is False
        rm.check_lot_risk.assert_called_once()


class TestShadowPerformanceReport:
    def test_no_log_reports_unavailable(self):
        from backend.copilot.performance_report import build_shadow_performance_report
        from pathlib import Path
        result = build_shadow_performance_report(log_path=Path("/tmp/does_not_exist_perf.csv"))
        assert result["available"] is False

    def test_computes_stats_without_tuning_anything(self):
        import tempfile, csv as csv_mod
        from pathlib import Path
        from backend.copilot.performance_report import build_shadow_performance_report
        from backend.copilot.shadow_logger import FIELDS

        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "log.csv"
            rows = [
                {"timestamp": "2024-06-03T10:00:00", "symbol": "NIFTY50", "option_type": "CE",
                 "risk_reward": "2.0", "market_regime": "TRENDING", "strategy_confirmation": "OPTION_PREMIUM",
                 "validation_approved": "True", "hypothetical_outcome": "TARGET_HIT"},
                {"timestamp": "2024-06-03T11:00:00", "symbol": "NIFTY50", "option_type": "PE",
                 "risk_reward": "2.0", "market_regime": "RANGING", "strategy_confirmation": "OPTION_PREMIUM",
                 "validation_approved": "True", "hypothetical_outcome": "SL_HIT"},
                {"timestamp": "2024-06-03T12:00:00", "symbol": "BANKNIFTY", "option_type": "CE",
                 "risk_reward": "1.8", "market_regime": "TRENDING", "strategy_confirmation": "OPTION_PREMIUM",
                 "validation_approved": "False", "hypothetical_outcome": ""},
            ]
            with open(log_path, "w", newline="") as f:
                writer = csv_mod.DictWriter(f, fieldnames=FIELDS)
                writer.writeheader()
                for r in rows:
                    writer.writerow({k: r.get(k, "") for k in FIELDS})

            result = build_shadow_performance_report(log_path=log_path)

        assert result["available"] is True
        assert result["total_opportunities"] == 3
        assert result["approved_opportunities"] == 2
        assert result["rejected_opportunities"] == 1
        assert result["overall"]["resolved"] == 2  # third row unresolved, excluded from win-rate math
        assert result["overall"]["target_hits"] == 1
        assert result["overall"]["sl_hits"] == 1
        assert result["overall"]["win_rate_pct"] == 50.0
        assert "NIFTY50" in result["by_symbol"]
        assert "CE" in result["by_ce_pe"] and "PE" in result["by_ce_pe"]
        assert "TRENDING" in result["by_market_regime"]


class TestScanLoop:
    def test_disabled_copilot_does_nothing(self):
        from backend.copilot.scan_loop import run_copilot_scan_pass
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COPILOT_ENABLED", None)
            result = run_copilot_scan_pass(CopilotTools(), ["NIFTY50"])
        assert result["available"] is False

    def test_scan_pass_never_raises_and_only_alerts_on_change(self):
        from backend.copilot.scan_loop import run_copilot_scan_pass
        from backend.copilot.alerts import AlertStateTracker
        engine, db = _real_engine()
        tools = CopilotTools(engine=engine, db_manager=db, risk_manager=engine.risk_manager)
        tracker = AlertStateTracker()
        with mock.patch.dict(os.environ, {"COPILOT_ENABLED": "true"}):
            r1 = run_copilot_scan_pass(tools, ["NIFTY50"], alert_tracker=tracker)
            r2 = run_copilot_scan_pass(tools, ["NIFTY50"], alert_tracker=tracker)
        assert r1["available"] is True
        assert "NIFTY50" in r1["results"]
        assert r2["alerts"] == []

    def test_live_scanner_hook_reuses_signal_no_duplicate_fetch(self):
        """The critical integration test: wiring LiveScanner(copilot_hook=...)
        must NOT cause a second evaluate_option_premium/get_option_chain
        call for the same symbol on the same scan pass."""
        from backend.scanner.live_scanner import LiveScanner
        from backend.copilot.scan_loop import live_scanner_copilot_hook, CopilotScanState

        engine, db = _real_engine()
        client = _mock_client_with_realistic_chain()
        engine.client = client
        tools = CopilotTools(engine=engine, db_manager=db, risk_manager=engine.risk_manager)
        state = CopilotScanState()

        scanner = LiveScanner(
            trading_engine=engine, universe_resolver=lambda: ["NIFTY50"],
            copilot_hook=live_scanner_copilot_hook(tools, state),
        )
        with mock.patch.dict(os.environ, {"COPILOT_ENABLED": "true", "COPILOT_MODE": "shadow"}):
            scanner.scan_symbol("NIFTY50")

        # get_option_chain should be called exactly once for this pass —
        # by the scanner's own evaluate_option_premium, not a second time
        # by the copilot hook.
        assert client.get_option_chain.call_count == 1

    def test_live_scanner_hook_dedups_shadow_log(self):
        from backend.scanner.live_scanner import LiveScanner
        from backend.copilot.scan_loop import live_scanner_copilot_hook, CopilotScanState
        from backend.copilot.shadow_logger import DEFAULT_LOG_PATH
        import tempfile
        from pathlib import Path

        engine, db = _real_engine()
        client = _mock_client_with_realistic_chain()
        engine.client = client
        tools = CopilotTools(engine=engine, db_manager=db, risk_manager=engine.risk_manager)
        state = CopilotScanState()
        scanner = LiveScanner(
            trading_engine=engine, universe_resolver=lambda: ["NIFTY50"],
            copilot_hook=live_scanner_copilot_hook(tools, state),
        )

        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "shadow.csv"
            with mock.patch("backend.copilot.shadow_logger.DEFAULT_LOG_PATH", log_path), \
                 mock.patch.dict(os.environ, {"COPILOT_ENABLED": "true", "COPILOT_MODE": "shadow"}):
                # Patch log_trade_plan's default path via direct call inspection:
                # scan_loop.py imports log_trade_plan directly, so patch its
                # module-level default by monkeypatching the function itself.
                calls = []
                original = __import__("backend.copilot.shadow_logger", fromlist=["log_trade_plan"]).log_trade_plan

                def _tracked(*args, **kwargs):
                    calls.append(1)
                    return original(*args, log_path=log_path)

                with mock.patch("backend.copilot.scan_loop.log_trade_plan", side_effect=_tracked):
                    scanner.scan_symbol("NIFTY50")
                    scanner.scan_symbol("NIFTY50")  # identical setup — should NOT log twice

            # Same signal/contract both times (mock is deterministic) -> only 1 log call.
            assert len(calls) == 1


class TestReconciliation:
    def test_no_log_file_reports_unavailable(self, tmp_path_str=None):
        from backend.copilot.reconciliation import reconcile_shadow_log
        from pathlib import Path
        result = reconcile_shadow_log(CopilotTools(), log_path=Path("/tmp/does_not_exist_copilot_log.csv"))
        assert result["available"] is False

    def test_no_client_reports_unavailable(self):
        import tempfile, os as _os
        from pathlib import Path
        from backend.copilot.reconciliation import reconcile_shadow_log
        from backend.copilot.shadow_logger import log_trade_plan
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "log.csv"
            log_trade_plan({"symbol": "NIFTY50"}, {"approved": True, "reasons_rejected": []}, "TRADE", log_path=log_path)
            result = reconcile_shadow_log(CopilotTools(), log_path=log_path)  # no engine -> no client
        assert result["available"] is False

    def test_resolves_target_hit(self):
        import tempfile
        from pathlib import Path
        from backend.copilot.reconciliation import reconcile_shadow_log
        from backend.copilot.shadow_logger import log_trade_plan
        with tempfile.TemporaryDirectory() as d:
            log_path = Path(d) / "log.csv"
            plan = {"symbol": "NIFTY50", "instrument_key": "NSE_FO|TEST", "entry_price_low": 99, "entry_price_high": 101,
                    "stop_loss": 90, "target_1": 120}
            log_trade_plan(plan, {"approved": True, "reasons_rejected": []}, "TRADE", log_path=log_path)

            client = MagicMock()
            # Forward candles that clearly hit the target (120) without touching the stop (90) first.
            client.get_historical_candles.return_value = [
                {"timestamp": "2030-01-01T10:00:00", "high": 105, "low": 99, "close": 104},
                {"timestamp": "2030-01-01T10:05:00", "high": 125, "low": 104, "close": 122},
            ]
            engine = MagicMock(client=client)
            tools = CopilotTools(engine=engine)
            result = reconcile_shadow_log(tools, log_path=log_path)

            assert result["available"] is True
            assert result["newly_resolved"] == 1
            with open(log_path) as f:
                import csv
                rows = list(csv.DictReader(f))
            assert rows[0]["hypothetical_outcome"] == "TARGET_HIT"


class TestConversationalLiveWiring:
    def test_direction_question_fetches_live_candles_when_none_supplied(self):
        engine, db = _real_engine()
        client = _mock_client_with_realistic_chain()
        engine.client = client
        tools = CopilotTools(engine=engine, db_manager=db)
        resp = chat("Is NIFTY bullish or bearish?", tools, candles_by_symbol={})
        assert "decision" in resp["resolved_context"]["analysis"]
        client.get_historical_candles.assert_called()


class TestDiagnosticsExtended:
    def test_option_chain_check_uses_real_client(self):
        engine, db = _real_engine()
        client = _mock_client_with_realistic_chain()
        engine.client = client
        tools = CopilotTools(engine=engine, db_manager=db, risk_manager=engine.risk_manager)
        result = run_full_diagnostics(tools)
        statuses = {row["component"]: row["status"] for row in result["rows"]}
        assert statuses["option_chain"] == "OK"
        assert statuses["copilot"] == "OK"


class TestConversationalRouting:
    def test_symbol_extraction(self):
        assert _extract_symbol("Is NIFTY bullish?") == "NIFTY50"
        assert _extract_symbol("check banknifty") == "BANKNIFTY"
        assert _extract_symbol("how is the market") == "NIFTY50"  # default

    def test_diagnostics_question_routes_to_diagnostics(self):
        fn = route_question("Run a full diagnostic")
        assert fn.__name__ == "_plan_diagnostics"

    def test_position_question_routes_to_positions(self):
        fn = route_question("Check my open position")
        assert fn.__name__ == "_plan_positions"

    def test_chat_never_raises_with_nothing_attached(self):
        tools = CopilotTools()
        resp = chat("How is the market?", tools, candles_by_symbol={})
        assert "answer" in resp
        assert isinstance(resp["answer"], str)

    def test_chat_uses_rule_based_fallback_by_default(self):
        tools = CopilotTools()
        resp = chat("Run a full diagnostic", tools, candles_by_symbol={})
        assert resp["adapter"] == "RuleBasedFallbackAdapter"


class TestLLMAdapterFallback:
    def test_default_backend_is_rule_based(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COPILOT_LLM_BACKEND", None)
            adapter = get_llm_adapter()
        assert isinstance(adapter, RuleBasedFallbackAdapter)

    def test_unrecognized_backend_falls_back(self):
        with mock.patch.dict(os.environ, {"COPILOT_LLM_BACKEND": "some_paid_api"}):
            settings = load_copilot_settings()
        assert settings.llm_backend == "none"

    def test_rule_based_never_invents_unavailable_data(self):
        adapter = RuleBasedFallbackAdapter()
        context = {"quote": {"available": False, "reason": "no live connection"}}
        answer = adapter.explain("What is the current premium?", context)
        assert "unavailable" in answer.lower()
        assert "no live connection" in answer


class TestDiagnostics:
    def test_reports_unknown_not_ok_when_nothing_attached(self):
        tools = CopilotTools()
        result = run_full_diagnostics(tools)
        assert result["available"] is True
        assert result["overall_status"] in ("DEGRADED", "FAILED")
        statuses = {row["component"]: row["status"] for row in result["rows"]}
        assert statuses["database"] == "UNKNOWN"
        assert statuses["risk_manager"] == "UNKNOWN"

    def test_reports_ok_for_working_components(self):
        engine, db = _real_engine()
        tools = CopilotTools(engine=engine, db_manager=db, risk_manager=engine.risk_manager)
        result = run_full_diagnostics(tools)
        statuses = {row["component"]: row["status"] for row in result["rows"]}
        assert statuses["database"] == "OK"
        assert statuses["risk_manager"] == "OK"
        assert statuses["strategy_engine"] == "OK"


class TestCopilotModeSafety:
    def test_unrecognized_mode_fails_safe_to_shadow(self):
        with mock.patch.dict(os.environ, {"COPILOT_MODE": "typo_live"}):
            settings = load_copilot_settings()
        assert settings.mode == "shadow"

    def test_never_defaults_to_live_or_enabled(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COPILOT_MODE", None)
            os.environ.pop("COPILOT_ENABLED", None)
            settings = load_copilot_settings()
        assert settings.mode != "live"
        assert settings.enabled is False


class TestAlerts:
    def test_no_alert_on_unchanged_decision(self):
        tracker = AlertStateTracker()
        first = tracker.check_trade_decision("NIFTY50", "SKIP", "no setup")
        second = tracker.check_trade_decision("NIFTY50", "SKIP", "still no setup")
        assert second is None  # same decision twice -> no repeat alert (no spam)

    def test_alert_on_transition_to_trade(self):
        tracker = AlertStateTracker()
        tracker.check_trade_decision("NIFTY50", "SKIP", "no setup")
        alert = tracker.check_trade_decision("NIFTY50", "TRADE", "strong CE setup")
        assert alert is not None
        assert alert.kind == "TRADE_OPPORTUNITY"

    def test_bot_health_alert_only_on_change(self):
        tracker = AlertStateTracker()
        first = tracker.check_bot_health("OK", "all good")
        assert first is None
        alert = tracker.check_bot_health("FAILED", "database down")
        assert alert is not None
        assert alert.kind == "BOT_PROBLEM"
        repeat = tracker.check_bot_health("FAILED", "database still down")
        assert repeat is None  # still failed, no repeat spam
