"""PHASE 5.3 tests — live readiness gate, BANKEX, reconciliation staleness,
current equity flow, bounded AI budget, order state machine invariants,
paper/live parity before broker submission.

V8-D parameters are untouched; paper behavior is exercised through the real
PaperTradingRuntime. No live order is ever placed by this module.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest import mock

import pytest

IST_TZ = timezone(timedelta(hours=5, minutes=30))
SESSION_NOW = datetime(2026, 9, 18, 10, 0, tzinfo=IST_TZ)  # Friday trading day

# ─────────────────────────────────────────────────────────────────────────────
# §2 BANKEX: universe validity + live contract metadata resolution
# ─────────────────────────────────────────────────────────────────────────────

def test_bankex_in_universe_and_metadata_tables():
    from backend.config.universe_config import (
        INDEX_EXCHANGE, INDEX_STRIKE_STEP, VALID_OPTION_INDICES,
    )
    assert VALID_OPTION_INDICES == [
        "NIFTY50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"]
    assert INDEX_EXCHANGE["BANKEX"] == "BSE"
    assert INDEX_EXCHANGE["SENSEX"] == "BSE"
    assert INDEX_EXCHANGE["NIFTY50"] == "NSE"


def test_bankex_static_fallback_key_present():
    from backend.broker.upstox_client import INDEX_TO_KEY
    assert INDEX_TO_KEY["BANKEX"] == "BSE_INDEX|BANKEX"


class _FakeMaster:
    """Instrument-master test double with LOT_SIZE from 'broker metadata'."""

    def __init__(self, lot_sizes: Dict[str, int]):
        self._lots = lot_sizes

    def metadata_for_key(self, instrument_key: str) -> Dict[str, Any]:
        lot = self._lots.get(instrument_key)
        return {"lot_size": lot} if lot else {}

    def get_instrument_metadata(self, instrument_key: str) -> Dict[str, Any]:
        return self.metadata_for_key(instrument_key)

    def status(self) -> Dict[str, Any]:
        return {"symbols_loaded": 6, "is_stale": False,
                "last_refreshed_seconds_ago": 120.0}


def _bankex_chain_row(lot_size: int = 0) -> Dict[str, Any]:
    row = {
        "strike": 52000.0, "option_type": "CE",
        "instrument_key": "BSE_FO|BANKEX_CE_TEST", "ltp": 210.5,
        "underlying_spot": 52105.0,
    }
    if lot_size:
        row["lot_size"] = lot_size
    return row


def test_bankex_contract_resolves_lot_size_from_broker_metadata():
    from backend.broker.contract_metadata import resolve_contract_metadata
    # Chain row WITHOUT lot size → resolver MUST take it from instrument master.
    rc = resolve_contract_metadata(
        underlying="BANKEX", contract=_bankex_chain_row(lot_size=0),
        expiry="2027-06-30", instrument_master=_FakeMaster({"BSE_FO|BANKEX_CE_TEST": 30}),
    )
    assert rc.lot_size == 30                       # broker metadata, never hardcoded
    assert rc.exchange == "BSE" and rc.exchange_segment == "BSE_FO"
    assert rc.tick_size is None                    # never invented
    assert rc.metadata_age_seconds == 120.0


def test_bankex_contract_chain_lot_wins_over_master_and_reports():
    from backend.broker.contract_metadata import resolve_contract_metadata
    rc = resolve_contract_metadata(
        underlying="BANKEX", contract=_bankex_chain_row(lot_size=35),
        expiry="2027-06-30", instrument_master=_FakeMaster({"BSE_FO|BANKEX_CE_TEST": 30}),
    )
    assert rc.lot_size == 35
    assert rc.warnings == []                       # chain lot present → no warning


def test_bankex_contract_rejects_wrong_exchange_segment():
    from backend.broker.contract_metadata import ContractResolutionError, resolve_contract_metadata
    bad = _bankex_chain_row(lot_size=30)
    bad["instrument_key"] = "NSE_FO|BANKEX_MISSEGMENTED"
    with pytest.raises(ContractResolutionError, match="exchange_mismatch"):
        resolve_contract_metadata(
            underlying="BANKEX", contract=bad, expiry="2027-06-30",
            instrument_master=_FakeMaster({}))


def test_bankex_contract_rejects_unresolved_lot_size():
    from backend.broker.contract_metadata import ContractResolutionError, resolve_contract_metadata
    with pytest.raises(ContractResolutionError, match="lot_size_unresolved"):
        resolve_contract_metadata(
            underlying="BANKEX", contract=_bankex_chain_row(lot_size=0),
            expiry="2027-06-30", instrument_master=_FakeMaster({}))


def test_bankex_contract_rejects_expired_and_guessed_expiry():
    from backend.broker.contract_metadata import ContractResolutionError, resolve_contract_metadata
    with pytest.raises(ContractResolutionError, match="contract_expired"):
        resolve_contract_metadata(
            underlying="BANKEX", contract=_bankex_chain_row(lot_size=30),
            expiry="2020-01-30", instrument_master=_FakeMaster({}))
    with pytest.raises(ContractResolutionError, match="expiry_unresolved"):
        resolve_contract_metadata(
            underlying="BANKEX", contract=_bankex_chain_row(lot_size=30),
            expiry="not-a-date", instrument_master=_FakeMaster({}))


def test_unsupported_underlying_refuses_not_substitutes():
    from backend.broker.contract_metadata import ContractResolutionError, resolve_contract_metadata
    with pytest.raises(ContractResolutionError, match="unsupported_underlying"):
        resolve_contract_metadata(
            underlying="NIFTYNEXT50", contract=_bankex_chain_row(lot_size=30),
            expiry="2027-06-30", instrument_master=_FakeMaster({}))


def test_backtest_router_accepts_bankex_symbol():
    """BANKEX passes the backtest symbol whitelist (universe gate open)."""
    from backend.config.universe_config import VALID_OPTION_INDICES
    invalid = [s for s in ["NIFTY50", "BANKEX", "NIFTYNEXT50"]
               if s.upper() not in VALID_OPTION_INDICES]
    assert invalid == ["NIFTYNEXT50"]


# ─────────────────────────────────────────────────────────────────────────────
# §10 live readiness gate
# ─────────────────────────────────────────────────────────────────────────────

def _settings_double(mode: str = "live"):
    from types import SimpleNamespace
    return SimpleNamespace(
        mode=mode,
        capital=SimpleNamespace(total=100000.0, max_allocation_per_trade=20.0),
        risk=SimpleNamespace(max_risk_per_trade_pct=2.5, max_daily_loss_pct=5.0,
                             max_trades_per_day=4, max_concurrent_positions=3),
        strategy=SimpleNamespace(name="V8_D_PULLBACK_ATM", exit_all_by="15:15"),
    )


def _db_with_reconcile(tmp_dir: str, ok: bool = True, fresh: bool = True):
    from backend.database.db_manager import DatabaseManager
    db = DatabaseManager(db_path=os.path.join(tmp_dir, "gate.db"))
    checked = datetime.now(timezone.utc)
    if not fresh:
        checked = checked - timedelta(hours=2)
    db.save_setting("paper_reconcile_ok", "1" if ok else "0")
    db.save_setting("paper_reconcile_detail", json.dumps(
        {"ok": ok, "checked_at": checked.isoformat()}))
    return db


def test_live_gate_blocks_when_nothing_configured(tmp_path):
    from backend.execution.live_gate import evaluate_live_readiness
    verdict = evaluate_live_readiness(client=None, db=None, settings=None)
    assert verdict.ready is False
    assert "upstox_auth" in verdict.blocked_reasons
    assert "reconciliation" in verdict.blocked_reasons


def test_live_gate_reports_each_check(tmp_path):
    from backend.execution.live_gate import evaluate_live_readiness
    verdict = evaluate_live_readiness(
        client=None, db=_db_with_reconcile(str(tmp_path)), settings=_settings_double(),
        require_funds=False)
    assert verdict.checks["reconciliation"]["ok"] is True
    assert verdict.checks["kill_switch"]["ok"] is True
    assert verdict.checks["strategy_valid"]["ok"] is True
    assert verdict.checks["upstox_auth"]["ok"] is False
    assert verdict.ready is False


def test_live_gate_stale_reconciliation_blocks(tmp_path):
    from backend.execution.live_gate import evaluate_live_readiness
    verdict = evaluate_live_readiness(
        client=None, db=_db_with_reconcile(str(tmp_path), ok=True, fresh=False),
        settings=_settings_double(), require_funds=False)
    assert verdict.checks["reconciliation"]["ok"] is False
    assert "reconciliation" in verdict.blocked_reasons
    assert verdict.checks["reconciliation"]["detail"].get("error") == "reconciliation_stale"


def test_live_gate_auth_via_fake_client(tmp_path):
    from backend.execution.live_gate import evaluate_live_readiness

    class FakeClient:
        def get_profile(self):
            return {"data": {"user_id": "U1"}}

        def get_funds(self):
            return {"available_margin": 50000.0, "used_margin": 0.0}

    verdict = evaluate_live_readiness(
        client=FakeClient(), db=_db_with_reconcile(str(tmp_path)),
        settings=_settings_double(), require_funds=True)
    assert verdict.checks["upstox_auth"]["ok"] is True
    assert verdict.checks["broker_funds"]["ok"] is True


# ─────────────────────────────────────────────────────────────────────────────
# §6 reconciliation staleness + §7 current equity in the scan path
# ─────────────────────────────────────────────────────────────────────────────

def _scan_env(path: str) -> dict:
    return {
        "TRADING_MODE": "paper",
        "TRADING_STRATEGY": "V8_D_PULLBACK_ATM",
        "UPSTOX_ORDER_PRODUCT": "I",
        "DATABASE_PATH": path,
        "RISK_PER_TRADE_PCT": "0.025",
        "TRADING_CAPITAL": "100000",
    }


class _BuyStrategy:
    name = "V8_D_PULLBACK_ATM"

    def __init__(self):
        self.last_equity = None

    def evaluate_v8d_signal(self, **kwargs):
        self.last_equity = kwargs.get("account_equity")
        from backend.strategy.signal import SignalType, StrategySignal
        sig = StrategySignal(
            strategy_name=self.name, symbol=kwargs["underlying_symbol"],
            signal=SignalType.BUY, entry_price=85.0, stop_loss=61.2, target=120.0,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
        sig.indicators = {
            "selected_contract": {
                "instrument_key": "NSE_FO|P53_CE", "option_type": "CE",
                "strike": 24000.0, "lot_size": 75, "freeze_quantity": 1800,
                "ltp": 85.0, "option_atr": 5.0, "atr": 5.0,
            },
            "sizing": {"quantity": 75},
            "underlying_spot": kwargs["spot_price"],
            "lot_size": 75, "option_type": "CE", "atm_strike": 24000,
        }
        return sig, type("L", (), {"decision": "ACCEPTED"})()


def _fresh_candles(n: int = 70):
    out = []
    for i in range(n):
        # last bar exactly at SESSION_NOW → quote_age ≈ 0 for the validator
        ts = SESSION_NOW - timedelta(minutes=5 * (n - 1 - i))
        out.append({"timestamp": ts.isoformat(), "open": 24000 + i,
                    "high": 24005 + i, "low": 23995 + i, "close": 24000 + i,
                    "volume": 1000})
    return out


def _chain():
    return [
        {"strike": 24000.0, "option_type": "CE", "instrument_key": "NSE_FO|P53_CE",
         "ltp": 85.0, "lot_size": 75, "freeze_quantity": 1800, "option_atr": 5.0,
         "atr": 5.0, "volume": 10000, "oi": 50000},
        {"strike": 24000.0, "option_type": "PE", "instrument_key": "NSE_FO|P53_PE",
         "ltp": 80.0, "lot_size": 75, "freeze_quantity": 1800, "option_atr": 5.0,
         "atr": 5.0, "volume": 8000, "oi": 40000},
    ]


class _FakeData:
    def __init__(self):
        self.candles = _fresh_candles()
        self.chain = _chain()
        self.spot = 24050.0
        self.expiry = "2027-01-07"

    def get_current_candles(self, symbol, interval="5minute", limit=120):
        return list(self.candles)

    def get_nearest_expiry(self, symbol):
        return self.expiry

    def get_option_chain_with_spot(self, symbol, expiry_date):
        return list(self.chain), self.spot


def _runtime_and_scanner(path, strategy):
    from backend.paper.market_scan_loop import PaperMarketScanner
    from backend.paper.paper_runtime import PaperTradingRuntime
    env = _scan_env(path)
    ctx = mock.patch.dict(os.environ, env, clear=False)
    ctx.start()
    rt = PaperTradingRuntime()
    rt.now_fn = lambda: SESSION_NOW + timedelta(minutes=165)  # 12:45 IST session
    scanner = PaperMarketScanner(data=_FakeData(), strategy=strategy, account_equity=100000)
    return rt, scanner, ctx


def test_scan_uses_current_equity_not_startup_capital():
    """§7: realized P&L changed the runtime equity → the strategy must see
    the CURRENT number, never the frozen TRADING_CAPITAL constant."""
    strategy = _BuyStrategy()
    rt, scanner, ctx = _runtime_and_scanner(
        os.path.join(tempfile.mkdtemp(), "eq.db"), strategy)
    try:
        rt.realized_equity = 104250.0   # simulated realized P&L
        res = scanner.scan_once(rt, now=SESSION_NOW)
        assert strategy.last_equity == 104250.0, strategy.last_equity
        assert res.scanned is True
    finally:
        ctx.stop()


def test_scan_blocks_when_reconciliation_stale():
    """§6: an old-but-OK verdict must yield typed RECONCILIATION_STALE."""
    strategy = _BuyStrategy()
    rt, scanner, ctx = _runtime_and_scanner(
        os.path.join(tempfile.mkdtemp(), "rec.db"), strategy)
    try:
        checked = datetime.now(timezone.utc) - timedelta(hours=3)
        rt.db.save_setting("paper_reconcile_ok", "1")
        rt.db.save_setting("paper_reconcile_detail", json.dumps(
            {"ok": True, "checked_at": checked.isoformat()}))
        res = scanner.scan_once(rt, now=SESSION_NOW)
        assert "RECONCILIATION_STALE" in res.reason, res.reason
        assert res.traded is False
    finally:
        ctx.stop()


def test_scan_reconciliation_failed_blocks_ai_call():
    """§5: reconciliation FAILED → the AI is never called; the typed
    RECONCILIATION_NOT_READY no-trade is returned before inference."""
    from backend.ai_decision.decision_engine import AITradingDecisionEngine

    class CountingProvider:
        calls = 0

        def chat_json(self, snapshot):
            type(self).calls += 1
            return json.dumps({"decision": "APPROVE", "confidence": 80,
                               "reason_codes": ["OK"]})

    strategy = _BuyStrategy()
    rt, scanner, ctx = _runtime_and_scanner(
        os.path.join(tempfile.mkdtemp(), "rec2.db"), strategy)
    try:
        engine = AITradingDecisionEngine(db=rt.db, provider=CountingProvider())
        engine.settings["enabled"] = True
        scanner.ai_engine = engine
        rt.db.save_setting("paper_reconcile_ok", "0")   # FAILED verdict
        rt.db.save_setting("paper_reconcile_detail", json.dumps(
            {"ok": False, "checked_at": datetime.now(timezone.utc).isoformat()}))
        res = scanner.scan_once(rt, now=SESSION_NOW)
        assert "RECONCILIATION_NOT_READY" in res.reason, res.reason
        assert CountingProvider.calls == 0              # AI never consulted
    finally:
        ctx.stop()


# ─────────────────────────────────────────────────────────────────────────────
# §8 AI toggle authority (DB override over env)
# ─────────────────────────────────────────────────────────────────────────────

def test_ai_toggle_override_beats_engine_default():
    from backend.api.routers.bot_control import ai_effectively_enabled

    class _Engine:
        enabled = False

    class _DB:
        _s: Dict[str, str] = {}

        def get_setting(self, key, default=""):
            return self._s.get(key, default)

        def save_setting(self, key, val):
            self._s[key] = val

    db = _DB()
    assert ai_effectively_enabled(db, _Engine()) is False
    db.save_setting("ai_decision_enabled_override", "1")
    assert ai_effectively_enabled(db, _Engine()) is True   # override wins
    db.save_setting("ai_decision_enabled_override", "0")
    assert ai_effectively_enabled(db, _Engine()) is False  # explicit OFF wins


def test_scan_gate_respects_ai_override_off(tmp_path):
    """§8: AI engine present+enabled but DB override OFF → V8-D proceeds
    with no AI call; override ON with engine disabled → AI still called."""
    from backend.ai_decision.decision_engine import AITradingDecisionEngine

    class CountingProvider:
        calls = 0

        def chat_json(self, snapshot):
            type(self).calls += 1
            return json.dumps({"decision": "REJECT", "confidence": 50,
                               "reason_codes": ["TEST_REJECT"]})

    strategy = _BuyStrategy()
    rt, scanner, ctx = _runtime_and_scanner(
        os.path.join(tempfile.mkdtemp(), "aitog.db"), strategy)
    try:
        rt.db.save_setting("paper_reconcile_ok", "1")
        rt.db.save_setting("paper_reconcile_detail", json.dumps(
            {"ok": True, "checked_at": datetime.now(timezone.utc).isoformat()}))
        rt.db.save_setting("ai_decision_enabled_override", "0")
        engine = AITradingDecisionEngine(db=rt.db, provider=CountingProvider())
        engine.settings["enabled"] = True
        scanner.ai_engine = engine
        res = scanner.scan_once(rt, now=SESSION_NOW)
        assert CountingProvider.calls == 0          # override OFF: no AI call
        # PHASE 5.3 QA: the scan 1 above may now have OPENED a paper position
        # (the new per-underlying duplicate guard sits BEFORE the AI gate,
        # matching backtest/live POSITION_ALREADY_OPEN ordering). Clear any
        # open position so scan 2 tests the AI override specifically.
        rt.broker.positions.clear()
        # Now flip ON via the same DB the API would write.
        rt.db.save_setting("ai_decision_enabled_override", "1")
        res = scanner.scan_once(rt, now=SESSION_NOW)
        assert CountingProvider.calls == 1          # override ON: AI called
        assert "AI_NO_TRADE:TEST_REJECT" in res.reason or res.reason.startswith("AI_NO_TRADE")
    finally:
        ctx.stop()


# ─────────────────────────────────────────────────────────────────────────────
# §12/§13 bounded AI budget
# ─────────────────────────────────────────────────────────────────────────────

def test_decide_with_budget_returns_wait_when_slow(tmp_path):
    from backend.ai_decision.contract import WAIT
    from backend.ai_decision.decision_engine import AITradingDecisionEngine
    from backend.tests.rehearsal_p52 import _test_signal
    from backend.ai_decision.setup_identity import build_setup_id

    class SlowProvider:
        def chat_json(self, snapshot):
            time.sleep(1.2)
            return json.dumps({"decision": "APPROVE", "confidence": 70,
                               "reason_codes": ["OK"]})

    eng = AITradingDecisionEngine(db=None, provider=SlowProvider())
    eng.settings["enabled"] = True
    sig, expiry = _test_signal(11)
    d = eng.decide_with_budget(
        max_wait_seconds=0.05, signal_id="budget-1",
        setup_id=build_setup_id(signal=sig, contract=sig.indicators["selected_contract"],
                                expiry=expiry, candles=[]),
        signal=sig, contract=sig.indicators["selected_contract"], expiry=expiry,
        candles=[], candles_fresh=True, candle_age_seconds=1.0,
        risk=_risk(), session=_session(), pipeline_strategy="V8_D_PULLBACK_ATM")
    assert d.decision == WAIT
    assert "AI_WAITING" in d.reason_codes
    assert d.allows_execution is False


def test_decide_with_budget_returns_real_decision_when_fast(tmp_path):
    from backend.ai_decision.decision_engine import AITradingDecisionEngine
    from backend.tests.rehearsal_p52 import _test_signal
    from backend.ai_decision.setup_identity import build_setup_id

    class FastProvider:
        def chat_json(self, snapshot):
            return json.dumps({"decision": "REJECT", "confidence": 40,
                               "reason_codes": ["NOPE"]})

    eng = AITradingDecisionEngine(db=None, provider=FastProvider())
    eng.settings["enabled"] = True
    sig, expiry = _test_signal(12)
    d = eng.decide_with_budget(
        max_wait_seconds=5.0, signal_id="budget-2",
        setup_id=build_setup_id(signal=sig, contract=sig.indicators["selected_contract"],
                                expiry=expiry, candles=[]),
        signal=sig, contract=sig.indicators["selected_contract"], expiry=expiry,
        candles=[], candles_fresh=True, candle_age_seconds=1.0,
        risk=_risk(), session=_session(), pipeline_strategy="V8_D_PULLBACK_ATM")
    assert d.decision == "REJECT"
    assert d.reason_codes and d.reason_codes[0] == "NOPE"


# ─────────────────────────────────────────────────────────────────────────────
# §5 order state machine + §23 paper/live parity
# ─────────────────────────────────────────────────────────────────────────────

def test_order_state_machine_covers_required_states():
    from backend.orders.order_state import (
        TERMINAL_STATES, OrderState, can_transition,
    )
    for name in ("CREATED", "SUBMITTED", "PARTIALLY_FILLED", "FILLED",
                 "CANCEL_REQUESTED", "CANCELLED", "REJECTED", "UNKNOWN"):
        assert hasattr(OrderState, name), name
    # Ambiguous path: SUBMITTED → UNKNOWN must be legal; UNKNOWN must never
    # auto-promote to FILLED without broker evidence (it is non-terminal).
    assert can_transition(OrderState.SUBMITTED, OrderState.UNKNOWN)
    assert OrderState.UNKNOWN not in TERMINAL_STATES


def test_normalize_broker_status_never_guesses():
    from backend.orders.order_models import OrderStatus, normalize_broker_status
    assert normalize_broker_status("complete") == OrderStatus.FILLED
    assert normalize_broker_status("open") == OrderStatus.OPEN
    assert normalize_broker_status("weird-nonsense") == OrderStatus.UNKNOWN


def test_live_order_manager_requires_product_and_client():
    from backend.orders.order_manager import OrderError, OrderManager
    from backend.orders.order_models import OrderRequest
    om = OrderManager(client=None, paper_mode=False)
    with pytest.raises(OrderError):
        om.place_order(OrderRequest(
            symbol="X", side="BUY", quantity=75, product=None, price=10.0))
    om2 = OrderManager(client=object(), paper_mode=False)
    with pytest.raises(OrderError):
        om2.place_order(OrderRequest(
            symbol="X", side="BUY", quantity=75, product="I", price=10.0))


def test_paper_live_parity_same_decision_and_quantity():
    """§23: same signal + account + contract → paper and live paths produce
    the SAME decision, quantity and risk outcome before broker submission."""
    from backend.database.db_manager import DatabaseManager
    from backend.execution.pipeline import ExecutionPipeline
    from backend.risk.risk_config import build_authoritative_risk_config

    def payload(qty):
        return {
            "timestamp": "2026-09-18T05:00:00+00:00", "underlying": "NIFTY50",
            "symbol": "NIFTY50", "instrument_key": "NSE_FO|PARITY_CE",
            "option_type": "CE", "strike": 24000.0, "expiry": "2027-01-07",
            "lot_size": 75, "premium": 85.0, "spot": 24050.0, "quantity": qty,
            "stop_loss": 61.2, "target": 120.0, "atr": 5.0, "side": "BUY",
            "order_type": "MARKET", "quote_age_seconds": 1.0,
            "strategy": "V8_D_PULLBACK_ATM",
        }

    risk_cfg = build_authoritative_risk_config(
        capital=100000.0, strategy_risk_pct=2.5, engine_risk_pct=2.5,
        risk_manager_daily_loss_pct=5.0, configured_risk_pct=2.5,
        allocation_limit_pct=20.0, max_daily_trades=4, max_positions=3,
        max_daily_loss_pct=5.0, lot_size_source="contract_metadata",
        order_product="I", strategy_name="V8_D_PULLBACK_ATM", eod_square_off="15:15")

    class FakeLiveBroker:
        """Positions book shape both paths agree on: flat."""

        def get_positions_with_details(self):
            return []

    def run(client_broker, paper_mode):
        placed = []

        def place(sig, sid):
            placed.append((sid, sig))
            from backend.orders.order_models import Order, OrderStatus
            return Order(id="oid-1", symbol="NSE_FO|PARITY_CE",
                         status=OrderStatus.FILLED, quantity=sig["quantity"],
                         filled_quantity=sig["quantity"], remaining_quantity=0,
                         price=sig["premium"], average_price=sig["premium"])

        pipe = ExecutionPipeline(
            strategy_name="V8_D_PULLBACK_ATM", risk=risk_cfg,
            db=DatabaseManager(db_path=os.path.join(tempfile.mkdtemp(), f"par-{paper_mode}.db")),
            place_order_fn=place, client=client_broker, token=None,
            require_live_token=False)
        res = pipe.submit_signal(payload(75))
        return res, placed

    # Paper path: no broker client (PaperRuntime supplies positions itself).
    # Live path: a real broker client exposing the positions book.
    res_paper, placed_paper = run(None, paper_mode=True)
    res_live, placed_live = run(FakeLiveBroker(), paper_mode=False)
    assert res_paper.accepted == res_live.accepted
    if res_paper.accepted:
        assert placed_paper[0][0] == placed_live[0][0]
        assert placed_paper[0][1]["quantity"] == placed_live[0][1]["quantity"]
        assert placed_paper[0][1]["instrument_key"] == placed_live[0][1]["instrument_key"]


# ─────────────────────────────────────────────────────────────────────────────
# §4 pooled HTTP transport for the AI provider
# ─────────────────────────────────────────────────────────────────────────────

def test_ai_provider_uses_pooled_session():
    from backend.ai_decision.decision_engine import OllamaDecisionProvider
    assert hasattr(OllamaDecisionProvider, "_session")
    s1 = OllamaDecisionProvider._session()
    s2 = OllamaDecisionProvider._session()
    assert s1 is s2  # one shared keep-alive session per process


def test_upstox_session_does_not_retry_post():
    """§5: money-moving POSTs must never be auto-retried by urllib3."""
    from backend.broker.upstox_client import _build_session
    try:
        session = _build_session()
    except Exception:
        pytest.skip("requests not installed")
    if session is None:
        pytest.skip("requests not installed")
    adapters = getattr(session, "adapters", {}) or {}
    adapter = adapters.get("https://")
    retry = getattr(adapter, "max_retries", None)
    if retry is None:
        pytest.skip("no urllib3 Retry configured")
    allowed = {m.upper() for m in (retry.allowed_methods or ())}
    assert "POST" not in allowed
    assert "GET" in allowed


def _risk():
    from backend.ai_decision.context import RiskContext
    return RiskContext(equity=100000.0, open_positions=0, trades_today=0,
                       daily_realized_pnl=0.0, kill_switch=False,
                       reconciliation_ok=True)


def _session():
    from backend.ai_decision.context import MarketSession
    return MarketSession(open=True, is_trading_day=True, label="P53_TEST")
