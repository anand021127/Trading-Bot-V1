"""PHASE B — Why-didn't-we-trade gate chain tests (spec §35-E, §8, §25).

backend/copilot/gate_chain.py is the ONE authority; the ai_decision router
delegates to it. Every typed stage must map from the exact reason strings the
paper worker persists, and the panel + Copilot must agree.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid

import pytest
from fastapi.testclient import TestClient

from backend.copilot.gate_chain import (
    _stage_from_reason,
    build_gate_chain_from_db,
    build_gate_chain_from_detail,
)
from backend.database.db_manager import DatabaseManager


@pytest.fixture()
def gc_db(monkeypatch):
    path = os.path.join(tempfile.gettempdir(), f"gatechain_{uuid.uuid4().hex}.db")
    db = DatabaseManager(db_path=path)
    db.init_db()
    monkeypatch.setenv("DATABASE_PATH", path)
    yield db
    from backend.config import runtime_config
    runtime_config.invalidate_runtime_config_cache()


def _detail(reason: str, *, signal="BUY", traded=False, extra=None):
    return {
        "scanned": True, "traded": traded, "reason": reason, "signal": signal,
        "details": extra if extra is not None else {},
    }


@pytest.mark.parametrize("reason,stage", [
    ("AI_NO_TRADE:AI_TIMEOUT", "AI_TIMEOUT"),
    ("AI_NO_TRADE:AI_PROVIDER_UNAVAILABLE", "AI_PROVIDER_UNAVAILABLE"),
    ("AI_NO_TRADE:AI_INVALID_RESPONSE", "AI_INVALID_RESPONSE"),
    ("AI_NO_TRADE:RECONCILIATION_NOT_READY", "RECONCILIATION_NOT_READY"),
    ("AI_NO_TRADE:RECONCILIATION_STALE", "RECONCILIATION_STALE"),
    ("AI_NO_TRADE:AI_WAITING", "AI_WAITING"),
    ("AI_NO_TRADE:ANYTHING_ELSE", "AI_REJECTED"),
    ("market_closed", "MARKET_CLOSED"),
    ("entry_window_closed:15:25-outside-09:20-14:45", "MARKET_CLOSED"),
    ("stale_candles_age_sec=1245", "STALE_DATA"),
    ("insufficient_candles:12<60", "STALE_DATA"),
    ("rejected:kill_switch=FULL_SYSTEM_STOP", "KILL_SWITCH"),
    ("rejected:MAX_DAILY_TRADES", "MAX_TRADES_REACHED"),
    ("rejected:MAX_POSITIONS", "MAX_EXPOSURE_REACHED"),
    ("rejected:MAX_DAILY_LOSS", "RISK_REJECTED"),
    ("rejected:INSUFFICIENT_EQUITY", "SIZING_REJECTED"),
    ("rejected:INVALID_LOT_SIZE", "NO_VALID_LOT_SIZE"),
    ("rejected:INVALID_CONTRACT:chain_size=0", "NO_VALID_CONTRACT"),
    ("rejected:BROKER_REJECTED — rate_limited", "BROKER_REJECTED"),
    ("rejected:INVALID_STRATEGY — mismatch", "EXECUTION_REJECTED"),
    ("no_trade:REJECT", "SIGNAL_REJECTED"),
    ("candle_fetch_error:HTTPError", "BROKER_UNAVAILABLE"),
    ("chain_fetch_error:Timeout", "BROKER_UNAVAILABLE"),
])
def test_typed_stage_mapping(reason, stage):
    assert _stage_from_reason(reason) == stage


def test_full_chain_example_ai_rejected():
    chain = build_gate_chain_from_detail(_detail(
        "AI_NO_TRADE:SOME_REASON", extra={
            "ai_decision": "REJECT", "ai_reason_codes": ["MOMENTUM_WEAK"],
            "rejection": [],
        }))
    g = chain["gates"]
    assert g["market"]["status"] == "OK"
    assert g["data"]["status"] == "OK"
    assert g["v8d_signal"]["status"] == "OK"
    assert g["ai_decision"]["status"] == "REJECTED"
    assert g["hard_risk"]["status"] == "NOT_EVALUATED"
    assert g["position_sizing"]["status"] == "NOT_EVALUATED"
    assert g["execution_pipeline"]["status"] == "NOT_ATTEMPTED"
    assert chain["final_reason"] == "AI_REJECTED"


def test_full_chain_example_max_trades():
    chain = build_gate_chain_from_detail(_detail("rejected:MAX_DAILY_TRADES"))
    g = chain["gates"]
    assert g["v8d_signal"]["status"] == "OK"
    assert g["ai_decision"]["status"] in ("SKIPPED", "NOT_EVALUATED")
    assert g["hard_risk"]["status"] == "REJECTED"
    assert chain["final_reason"] == "MAX_TRADES_REACHED"


def test_full_chain_example_traded():
    chain = build_gate_chain_from_detail(_detail(
        "submitted", traded=True,
        extra={"signal_id": "sid-1", "instrument_key": "NSE_FO|1|01-10-2025",
               "premium": 135.45, "quantity": 40}))
    assert chain["traded"] is True
    assert chain["final_reason"] == "TRADED"
    g = chain["gates"]
    assert g["broker_paper_execution"]["status"] == "OK"
    assert g["execution_pipeline"]["status"] == "OK"


def test_full_chain_v8d_no_signal():
    chain = build_gate_chain_from_detail(_detail(
        "no_trade:REJECT", signal=None,
        extra={"rejection": ["EMA20/EMA50 pullback criteria not satisfied"]}))
    assert chain["final_reason"] == "SIGNAL_REJECTED"
    assert chain["v8d_rejection_reasons"] == ["EMA20/EMA50 pullback criteria not satisfied"]
    assert "pullback" in chain["human_summary"]


def test_honest_empty_state(gc_db):
    chain = build_gate_chain_from_db(gc_db)
    assert chain["available"] is False
    assert "No scan has been recorded since bot startup" in chain["reason"]
    assert chain["stage"] == "UNKNOWN"
    assert chain["gates"] == {}


def test_ai_decision_router_delegates_to_gate_chain(gc_db):
    gc_db.save_setting("paper_worker_last_scan_detail", json.dumps(
        _detail("rejected:MAX_DAILY_TRADES")))
    from backend.api.routers.ai_decision import get_why_not_traded
    out = get_why_not_traded()
    assert out["available"] is True
    assert out["stage"] == "MAX_TRADES_REACHED"
    assert out["breakdown"]["stage"] == "MAX_TRADES_REACHED"   # legacy panel contract
    assert out["gates"]["hard_risk"]["status"] == "REJECTED"


def test_why_not_traded_endpoint_honest_when_empty(gc_db):
    from backend.api.routers.ai_decision import get_why_not_traded
    out = get_why_not_traded()
    assert out["available"] is False
    assert "No scan has been recorded" in out["reason"]


def test_panel_and_copilot_agree(gc_db):
    """The ai_decision panel payload and the Copilot context gate chain must
    report the SAME stage for the same persisted scan (spec §34)."""
    gc_db.save_setting("paper_worker_last_scan_detail", json.dumps(
        _detail("AI_NO_TRADE:AI_TIMEOUT")))
    from backend.api.routers.ai_decision import get_why_not_traded
    panel = get_why_not_traded()
    copilot = build_gate_chain_from_db(gc_db)
    assert panel["stage"] == copilot["stage"] == "AI_TIMEOUT"
