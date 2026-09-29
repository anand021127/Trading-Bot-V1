"""PHASE B — Copilot context endpoint tests (spec §35-A, §27-§30).

The ONE authoritative context (GET /api/copilot/context +
backend/copilot/full_context.py) must contain every required section, be
bounded, carry source/freshness labels, state unavailable data honestly and
NEVER include secrets.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid

import pytest
from fastapi.testclient import TestClient

from backend.database.db_manager import DatabaseManager


@pytest.fixture()
def ctx_db(monkeypatch):
    path = os.path.join(tempfile.gettempdir(), f"copctx_{uuid.uuid4().hex}.db")
    db = DatabaseManager(db_path=path)
    db.init_db()
    monkeypatch.setenv("DATABASE_PATH", path)
    from backend.config import runtime_config
    runtime_config.set_runtime_config_db(db)
    monkeypatch.setenv("TRADING_STRATEGY", "V8_D_PULLBACK_ATM")
    monkeypatch.setenv("TRADING_MODE", "paper")
    yield db
    from backend.config import runtime_config
    runtime_config.invalidate_runtime_config_cache()
    runtime_config.set_runtime_config_db(None)


def _client() -> TestClient:
    from backend.api.main import app
    return TestClient(app)


def _context() -> dict:
    from backend.copilot.full_context import build_full_context
    return build_full_context(app_state=None, include_errors=True)


def test_context_endpoint_exists_and_200(ctx_db):
    r = _client().get("/api/copilot/context")
    assert r.status_code == 200
    body = r.json()
    assert body["generated_at"]


def test_context_contains_strategy_mode_capital_maxtrades(ctx_db):
    ctx = _context()
    assert ctx["bot"]["strategy"] == "V8_D_PULLBACK_ATM"
    assert ctx["bot"]["mode"] == "paper"
    assert ctx["bot"]["broker"] == "UPSTOX"
    assert "option_indices" in ctx["bot"]["supported_instruments"]
    assert ctx["configuration"]["capital"]["starting_capital"] > 0
    assert ctx["configuration"]["risk"]["max_trades_per_day"] > 0
    assert ctx["configuration"]["mode"] == "paper"


def test_context_contains_scanner_signal_rejection_chain(ctx_db):
    ctx = _context()
    # Scanner + signal + rejection sections exist with honest availability
    for key in ("scanner", "latest_signal", "latest_rejection"):
        assert key in ctx
        assert "source" in ctx[key] and "as_of" in ctx[key]
    # With no scan persisted: available=False and the EXACT reason (§30)
    assert ctx["latest_signal"]["available"] is False
    assert "No actionable V8-D signal has been recorded" in ctx["latest_signal"]["reason"]
    assert ctx["latest_rejection"]["available"] is False


def test_context_gate_chain_after_scan(ctx_db):
    """A persisted scan record yields the full MARKET→...→RECONCILIATION chain."""
    ctx_db.save_setting("paper_worker_last_scan_detail", json.dumps({
        "scanned": True, "traded": False, "reason": "AI_NO_TRADE:AI_TIMEOUT",
        "signal": "BUY",
        "details": {"ai_decision": "WAIT", "rejection": ["pre-AI ok"],
                    "recorded_at": "2026-09-28T05:00:00+00:00"},
    }))
    ctx = _context()
    rej = ctx["latest_rejection"]
    assert rej["available"] is True
    chain = rej["gate_chain"]
    assert chain["stage"] == "AI_TIMEOUT"
    gates = chain["gates"]
    for gate in ("market", "data", "v8d_signal", "ai_decision", "hard_risk",
                 "position_sizing", "contract_validation", "execution_pipeline",
                 "broker_paper_execution", "reconciliation"):
        assert gate in gates, f"missing gate {gate}"
        assert gates[gate]["status"] in ("OK", "REJECTED", "NOT_EVALUATED",
                                         "NOT_ATTEMPTED", "SKIPPED", "UNKNOWN")
    assert gates["v8d_signal"]["status"] == "OK"
    assert gates["hard_risk"]["status"] == "NOT_EVALUATED"  # AI failed first
    assert chain["human_summary"]


def test_context_today_recent_trades_positions(ctx_db):
    ctx = _context()
    assert ctx["today"]["available"] is True
    assert ctx["today"]["configured_max_trades"] > 0
    assert ctx["recent_trades"]["available"] is True
    assert ctx["recent_trades"]["count"] <= 20          # bounded (§27)
    assert ctx["positions"]["available"] is True


def test_context_risk_reconciliation_backtest_sections(ctx_db):
    ctx = _context()
    assert ctx["risk"]["available"] is True
    assert ctx["risk"]["max_trades"] > 0
    assert ctx["reconciliation"]["available"] is True
    assert ctx["reconciliation"]["state"] in ("OK", "FAILED", "NEVER_CHECKED")
    assert ctx["backtest"]["available"] is False        # honest empty state
    assert "stored" in ctx["backtest"]["reason"].lower()
    assert ctx["configuration_mismatches"] == []
    assert ctx["errors"]["available"] is True


def test_context_websocket_and_ai_sections(ctx_db):
    ctx = _context()
    ws = ctx["websocket"]
    assert "source" in ws
    assert ctx["ai"]["available"] is True
    assert ctx["ai"]["enabled"] in (True, False)
    assert "SEPARATE function" in ctx["ai"]["important"] or "SEPARATE" in ctx["ai"]["important"]
    assert ctx["copilot"]["role"].startswith("READ-ONLY")


def test_context_secrets_never_returned(ctx_db):
    secret = "super-secret-access-token-abc123"
    ctx_db.save_setting("upstox_access_token", secret)
    ctx_db.save_setting("control_token", "ctrl-secret-xyz")
    ctx = _context()
    raw = json.dumps(ctx)
    assert secret not in raw
    assert "ctrl-secret-xyz" not in raw
    broker = ctx["broker"]
    assert "token_value" not in json.dumps(broker)
    # fingerprint-style metadata is fine; raw token is not present anywhere
    assert secret not in json.dumps(broker)


def test_context_endpoint_no_secrets_over_http(ctx_db):
    secret = "http-level-secret-token-999"
    ctx_db.save_setting("upstox_access_token", secret)
    r = _client().get("/api/copilot/context")
    assert r.status_code == 200
    assert secret not in r.text


def test_backtest_summary_is_dynamic_not_hardcoded(ctx_db):
    """Numbers must come from the stored result — a seeded job appears, an
    empty store reports unavailable (never §23's example numbers)."""
    ctx = _context()
    assert ctx["backtest"]["available"] is False
    assert "171" not in json.dumps(ctx["backtest"])
    assert "45397" not in json.dumps(ctx["backtest"])
    from backend.backtest import job_store
    # set_result is an UPDATE — the job row must exist first or the write
    # silently no-ops. Seed via the public API: create_job(...) → set_result(...).
    job_store.job_store.clear_all()
    job_store.job_store.create_job(
        strategies=["V8_D_PULLBACK_ATM"],
        symbols=["NIFTY50"],
        start_date="2026-09-01",
        end_date="2026-09-30",
        interval="day",
        capital=20000.0,
        job_id="test-job-ctx-1",
    )
    job_store.job_store.set_result(
        "test-job-ctx-1",
        {
            "total_trades": 42, "winning_trades": 11, "losing_trades": 31,
            "win_rate_pct": 26.19, "net_profit": -1234.5, "profit_factor": 0.7,
            "max_drawdown_pct": 12.5, "rejection_reason_counts":
                {"Daily trade limit reached: 3/3": 100},
            "data_source": "real_upstox_v3", "data_coverage_pct": 99.0,
        },
    )
    ctx2 = _context()
    assert ctx2["backtest"]["available"] is True
    assert ctx2["backtest"]["summary"]["trades"] == 42
    assert ctx2["backtest"]["config"]["capital"] is None or isinstance(
        ctx2["backtest"]["config"]["capital"], float)
    assert ctx2["backtest"]["rejection_reason_counts"]["Daily trade limit reached: 3/3"] == 100
    job_store.job_store.clear_all()


def test_context_source_labels_present(ctx_db):
    ctx = _context()
    allowed = {"LIVE_RUNTIME", "DATABASE", "CONFIGURATION", "BACKTEST", "BROKER",
               "WEBSOCKET", "SCANNER", "RISK_MANAGER"}
    for key in ("bot", "configuration", "market", "data_health", "websocket",
                "scanner", "latest_signal", "today", "positions",
                "recent_trades", "risk", "reconciliation", "broker", "ai",
                "backtest"):
        assert ctx[key].get("source") in allowed, f"{key} missing source label"
        assert ctx[key].get("as_of"), f"{key} missing as_of"


def test_context_freshness_age_fields(ctx_db):
    ctx_db.save_setting("paper_worker_last_scan_detail", json.dumps({
        "scanned": True, "traded": True, "reason": "submitted",
        "signal": "BUY", "details": {"recorded_at": "2026-09-28T05:00:00+00:00"},
    }))
    ctx = _context()
    chain = ctx["latest_rejection"]["gate_chain"]
    assert "age_seconds" in chain
    assert ctx["today"]["as_of"]
