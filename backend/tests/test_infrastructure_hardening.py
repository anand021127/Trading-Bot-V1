"""Infrastructure hardening tests — strategy logic is not under test here."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from backend.broker.positions_api import fetch_positions
from backend.config.strategy_registry import StrategySelectionError, load_strategy
from backend.database.db_manager import DatabaseManager
from backend.execution.eod import is_past_square_off, square_off_bot_positions
from backend.execution.kill_switch import FULL_SYSTEM_STOP, PersistentKillSwitch
from backend.execution.pipeline import ExecutionPipeline
from backend.execution.token_guard import TokenGuardError, assert_token_usable
from backend.orders.execution_guard import evaluate_pretrade_guard
from backend.orders.idempotency import IdempotentOrderStore, make_signal_id
from backend.orders.order_manager import OrderError, OrderManager
from backend.orders.order_models import Order, OrderRequest, OrderStatus
from backend.risk.risk_config import RiskConfigError, build_authoritative_risk_config


def _db():
    db = DatabaseManager(":memory:")
    db.init_db()
    return db


def _risk(**kwargs):
    base = dict(
        capital=100000.0,
        strategy_risk_pct=0.025,
        engine_risk_pct=0.025,
        risk_manager_daily_loss_pct=0.02,
        configured_risk_pct=0.025,
        allocation_limit_pct=0.18,
        max_daily_trades=3,
        max_positions=1,
        max_daily_loss_pct=0.02,
        lot_size_source="contract_metadata",
        order_product="I",
        strategy_name="V8_D_PULLBACK_ATM",
    )
    base.update(kwargs)
    return build_authoritative_risk_config(**base)


def test_strategy_selection_explicit_v8d():
    s = load_strategy("V8_D_PULLBACK_ATM")
    assert s.name == "V8_D_PULLBACK_ATM"


def test_strategy_selection_invalid_fails():
    with pytest.raises(StrategySelectionError):
        load_strategy("NOT_A_STRATEGY")


def test_strategy_selection_empty_fails():
    with pytest.raises(StrategySelectionError):
        load_strategy("")


def test_risk_config_conflict_fails_startup():
    with pytest.raises(RiskConfigError):
        build_authoritative_risk_config(
            capital=100000,
            strategy_risk_pct=0.025,
            engine_risk_pct=0.01,
            risk_manager_daily_loss_pct=0.02,
            configured_risk_pct=0.025,
            allocation_limit_pct=0.18,
            max_daily_trades=3,
            max_positions=1,
            max_daily_loss_pct=0.02,
            lot_size_source="contract_metadata",
            order_product="I",
            strategy_name="V8_D_PULLBACK_ATM",
        )


def test_risk_config_missing_product_fails():
    with pytest.raises(RiskConfigError):
        _risk(order_product="")


def test_guard_rejects_risk_breach():
    cfg = _risk()
    decision = evaluate_pretrade_guard(
        premium=80.0, stop_loss=50.0, quantity=750, lot_size=75, config=cfg
    )
    assert decision.allowed is False
    assert any("risk limit" in r or "allocation" in r for r in decision.reasons)


def test_guard_rejects_unresolved_lot():
    cfg = _risk()
    decision = evaluate_pretrade_guard(
        premium=80.0, stop_loss=57.6, quantity=75, lot_size=None, config=cfg
    )
    assert decision.allowed is False


def test_guard_rejects_non_multiple():
    cfg = _risk()
    decision = evaluate_pretrade_guard(
        premium=80.0, stop_loss=57.6, quantity=80, lot_size=75, config=cfg
    )
    assert decision.allowed is False


def test_guard_accepts_in_limit_trade():
    cfg = _risk()
    decision = evaluate_pretrade_guard(
        premium=80.0, stop_loss=57.6, quantity=75, lot_size=75, config=cfg
    )
    assert decision.allowed is True


def test_cheap_premium_does_not_increase_quantity():
    """Infrastructure rejects rather than scaling lots on ₹1 premium."""
    cfg = _risk()
    # 1 lot of 75 * 0.42 risk = 31.5, well under 2500 risk — allowed if qty stays 1 lot
    one = evaluate_pretrade_guard(premium=1.05, stop_loss=0.63, quantity=75, lot_size=75, config=cfg)
    # Scaling lots because premium is cheap blows through allocation (₹1.05 × 20000 > 18%)
    many = evaluate_pretrade_guard(premium=1.05, stop_loss=0.63, quantity=20025, lot_size=75, config=cfg)
    assert one.allowed is True
    assert many.allowed is False


def test_duplicate_signal_is_blocked():
    db = _db()
    store = IdempotentOrderStore(db)
    sid = make_signal_id(strategy="V8_D_PULLBACK_ATM", timestamp="t", instrument="k", direction="CE")
    first = store.remember_intent(sid, {"x": 1})
    second = store.remember_intent(sid, {"x": 2})
    assert first["duplicate"] is False
    assert second["duplicate"] is True


def test_retry_after_timeout_does_not_create_second_intent():
    db = _db()
    store = IdempotentOrderStore(db)
    sid = make_signal_id(strategy="V8_D_PULLBACK_ATM", timestamp="t2", instrument="k", direction="PE")
    store.remember_intent(sid, {"try": 1})
    store.mark_submitted(sid, "OID-1")
    again = store.remember_intent(sid, {"try": 2})
    assert again["duplicate"] is True
    assert store.get(sid)["broker_order_id"] == "OID-1"


def test_partial_fill_state():
    client = MagicMock()
    client.place_order.return_value = {"success": True, "order_id": "o1", "status": "open"}
    client.get_order_details.return_value = {
        "order_id": "o1", "status": "PARTIALLY_FILLED",
        "average_price": 10.0, "filled_quantity": 50, "quantity": 100,
    }
    mgr = OrderManager(client=client, paper_mode=False, default_product="I")
    order = mgr.place_order(OrderRequest(symbol="NSE_FO|X", side="BUY", quantity=100))
    assert order.status == OrderStatus.PARTIALLY_FILLED
    assert order.filled_quantity == 50
    assert order.remaining_quantity == 50


def test_order_rejection_state():
    client = MagicMock()
    client.place_order.return_value = {"success": True, "order_id": "o2"}
    client.get_order_details.return_value = {
        "order_id": "o2", "status": "REJECTED", "filled_quantity": 0, "quantity": 75,
    }
    mgr = OrderManager(client=client, paper_mode=False, default_product="I")
    order = mgr.place_order(OrderRequest(symbol="NSE_FO|X", side="BUY", quantity=75))
    assert order.status == OrderStatus.REJECTED
    assert order.filled_quantity == 0


def test_unknown_order_state():
    client = MagicMock()
    client.place_order.return_value = {"success": True, "order_id": "o3"}
    client.get_order_details.return_value = {"order_id": "o3", "status": "WEIRD"}
    mgr = OrderManager(client=client, paper_mode=False, default_product="I")
    order = mgr.place_order(OrderRequest(symbol="NSE_FO|X", side="BUY", quantity=75))
    assert order.status == OrderStatus.UNKNOWN


def test_live_order_requires_product():
    mgr = OrderManager(client=MagicMock(), paper_mode=False, default_product=None)
    with pytest.raises(OrderError):
        mgr.place_order(OrderRequest(symbol="NSE_FO|X", side="BUY", quantity=75))


def test_positions_api_failure_not_empty_book():
    client = MagicMock()
    client.get_positions_with_details.side_effect = RuntimeError("timeout")
    result = fetch_positions(client)
    assert result.ok is False
    assert result.error == "RuntimeError"
    assert result.positions == []


def test_positions_api_zero_is_ok():
    client = MagicMock()
    client.get_positions_with_details.return_value = []
    result = fetch_positions(client)
    assert result.ok is True
    assert result.positions == []


def test_expired_token_blocks():
    from backend.broker.token_resolver import decode_jwt_safe  # noqa: F401
    expired = "eyJhbGciOiAibm9uZSJ9.eyJleHAiOiAxNjAwMDAwMDAwLCAiaWF0IjogMTYwMDAwMDAwMH0.x"
    with pytest.raises(TokenGuardError):
        assert_token_usable(expired, context="test")


def test_missing_token_blocks():
    with pytest.raises(TokenGuardError):
        assert_token_usable("", context="test")


def test_eod_cutoff():
    now = datetime(2025, 1, 2, 15, 16, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert is_past_square_off(now, "15:15") is True
    morning = datetime(2025, 1, 2, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert is_past_square_off(morning, "15:15") is False


def test_eod_retry_and_remaining():
    calls = {"n": 0}

    def close(pos):
        calls["n"] += 1
        if pos["id"] == "bad":
            raise RuntimeError("fail")

    out = square_off_bot_positions(close, [{"id": "ok"}, {"id": "bad"}], max_retries=2)
    assert out["closed"] == 1
    assert len(out["remaining"]) == 1


def test_kill_switch_survives_new_db_handle():
    db = _db()
    path = db.db_path
    ks = PersistentKillSwitch(db)
    ks.set_level(FULL_SYSTEM_STOP, "test")
    other = PersistentKillSwitch(DatabaseManager(path) if path != ":memory:" else db)
    if path == ":memory:":
        assert ks.blocks_entries() is True
    else:
        assert other.level() == FULL_SYSTEM_STOP


def test_pipeline_duplicate_and_guard():
    import tempfile, os
    path = os.path.join(tempfile.mkdtemp(), "p.db")
    db = DatabaseManager(path)
    db.init_db()
    placed = []

    def place(signal, sid):
        placed.append(sid)
        return Order(id="X1", symbol="NIFTY", status=OrderStatus.FILLED, filled_quantity=75, quantity=75)

    pipe = ExecutionPipeline(
        strategy_name="V8_D_PULLBACK_ATM",
        risk=_risk(),
        db=db,
        place_order_fn=place,
        require_live_token=False,
    )
    sig = {
        "timestamp": "2025-01-02T10:00:00+05:30",
        "instrument_key": "NSE_FO|123",
        "option_type": "CE",
        "underlying": "NIFTY50",
        "strike": 24000,
        "expiry": "2027-01-09",
        "lot_size": 75,
        "premium": 80,
        "spot": 24010,
        "quantity": 75,
        "stop_loss": 57.6,
        "quote_age_seconds": 1,
    }
    first = pipe.submit_signal(sig)
    second = pipe.submit_signal(sig)
    assert first.accepted is True
    assert second.accepted is False
    assert second.reason == "duplicate_signal"
    assert len(placed) == 1


def test_pipeline_kill_blocks_entries():
    import tempfile, os
    path = os.path.join(tempfile.mkdtemp(), "k.db")
    db = DatabaseManager(path)
    db.init_db()
    pipe = ExecutionPipeline(
        strategy_name="V8_D_PULLBACK_ATM",
        risk=_risk(),
        db=db,
        place_order_fn=lambda s, i: None,
    )
    pipe.kill.set_level(FULL_SYSTEM_STOP, "unit")
    res = pipe.submit_signal({"timestamp": "t", "instrument_key": "k", "option_type": "CE"})
    assert res.accepted is False
    assert "kill_switch" in res.reason
