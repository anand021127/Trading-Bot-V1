"""PHASE B — Operations API, performance endpoint and health-surface tests
(spec §35-C, §35-D, §35-F).

- Operations API must 200 on /api/bot/operations|ai-toggle|mode|kill and the
  frontend must only call /api/bot/* paths.
- Performance endpoint must 200 on an empty database (the missing
  performance_snapshots table used to 500 -> frontend "Network Error").
- Health surfaces must degrade honestly (never UNKNOWN->HEALTHY).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import uuid

import pytest
from fastapi.testclient import TestClient

from backend.database.db_manager import DatabaseManager


@pytest.fixture()
def ops_db(monkeypatch):
    path = os.path.join(tempfile.gettempdir(), f"opsperf_{uuid.uuid4().hex}.db")
    db = DatabaseManager(db_path=path)
    db.init_db()
    monkeypatch.setenv("DATABASE_PATH", path)
    from backend.config import runtime_config
    runtime_config.set_runtime_config_db(db)
    yield db
    from backend.config import runtime_config
    runtime_config.invalidate_runtime_config_cache()
    runtime_config.set_runtime_config_db(None)


def _client() -> TestClient:
    from backend.api.main import app
    return TestClient(app)


# ── C. Operations API ──────────────────────────────────────────────────
def test_operations_endpoint_200(ops_db):
    r = _client().get("/api/bot/operations")
    assert r.status_code == 200
    body = r.json()
    for key in ("mode", "strategy", "market", "reconciliation", "kill_switch",
                "ai", "live_readiness", "runtime_config", "bot_running"):
        assert key in body


def test_ai_toggle_roundtrip(ops_db):
    c = _client()
    r1 = c.post("/api/bot/ai-toggle", json={"enabled": True})
    assert r1.status_code == 200
    assert r1.json()["success"] is True
    assert ops_db.get_setting("ai_decision_enabled_override", "") == "1"
    r2 = c.post("/api/bot/ai-toggle", json={"enabled": False})
    assert r2.status_code == 200
    assert ops_db.get_setting("ai_decision_enabled_override", "") == "0"


def test_mode_switch_paper_works(ops_db):
    r = _client().post("/api/bot/mode", json={"mode": "paper"})
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_mode_switch_live_blocked_by_gate(ops_db):
    """LIVE must stay gated by the existing live readiness verdict."""
    r = _client().post("/api/bot/mode", json={"mode": "live"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is False
    assert body.get("blocked_reasons")


def test_kill_and_reset(ops_db):
    c = _client()
    r = c.post("/api/bot/kill")
    assert r.status_code == 200
    assert r.json()["success"] is True
    r2 = c.post("/api/bot/reset-kill")
    assert r2.status_code == 200


def test_frontend_uses_api_prefixed_bot_paths():
    """§35-C: the frontend must not emit bare /bot/* requests (the 404 bug)."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    src = open(os.path.join(root, "frontend", "src", "pages", "Operations.tsx"),
               encoding="utf-8").read()
    assert "'/bot/" not in src and '"/bot/' not in src
    for path in ("/api/bot/operations", "/api/bot/ai-toggle",
                 "/api/bot/mode", "/api/bot/kill"):
        assert path in src
    # endpoints.ts must use /api-prefixed control paths too
    ep = open(os.path.join(root, "frontend", "src", "api", "endpoints.ts"),
              encoding="utf-8").read()
    assert "api.post('/api/bot/kill')" in ep
    assert "'/api/bot/status'" in ep


def test_operations_runtime_config_consistency(ops_db):
    """§34: Operations and Overview must report the same authoritative config."""
    ops_db.save_settings_blob({
        "mode": "paper",
        "capital": {"total": 20000},
        "risk": {"max_trades_per_day": 20},
    })
    c = _client()
    ops = c.get("/api/bot/operations").json()
    ov = c.get("/api/overview").json()
    assert ops["runtime_config"]["capital"]["starting_capital"] == \
        ov["capital"]["total"] == 20000.0
    assert ops["runtime_config"]["risk"]["max_trades_per_day"] == 20
    assert ops["runtime_config"]["capital"]["source"] == "sqlite_settings"
    assert ov["capital"]["source"] == "runtime_config"


# ── D. Performance endpoint ────────────────────────────────────────────
def test_performance_snapshots_table_created_by_init_db(ops_db):
    cols = [r["name"] for r in
            ops_db._connect().execute("PRAGMA table_info(performance_snapshots)").fetchall()]
    assert {"id", "date", "equity", "net_pnl", "total_trades", "win_rate",
            "max_drawdown_pct", "created_at"} <= set(cols)


def test_performance_endpoint_200_on_empty_db(ops_db):
    r = _client().get("/api/performance")
    assert r.status_code == 200
    body = r.json()
    assert body["metrics"] is None
    assert body["equity_curve"] == []
    assert body["monthly_returns"] == {}


def test_performance_endpoint_200_when_snapshots_exist(ops_db):
    ops_db.insert_performance_snapshot(
        date="2026-09-28", equity=20000.0, net_pnl=-120.0, total_trades=3,
        win_rate=33.3, max_drawdown_pct=1.2,
    )
    from backend.api.routers import performance as perf_mod
    original = perf_mod.db_manager
    perf_mod.db_manager = ops_db
    try:
        r = _client().get("/api/performance")
    finally:
        perf_mod.db_manager = original
    assert r.status_code == 200
    body = r.json()
    assert body["total_snapshots"] == 1
    assert body["performance"][0]["net_pnl"] == -120.0


def test_performance_endpoint_metrics_from_trades(ops_db):
    from datetime import datetime, timezone
    from backend.database.models import Trade
    now = datetime.now(timezone.utc)
    ids = []
    for pnl in (100.0, -40.0, 60.0):
        tid = f"perf-{uuid.uuid4().hex[:8]}"
        ids.append(tid)
        ops_db.insert_trade(Trade(
            id=tid, symbol="NIFTY50", side="BUY",
            quantity=10, price=100.0, timestamp=now, strategy="V8_D_PULLBACK_ATM",
            status="closed", pnl=pnl,
        ))
    for tid, pnl in zip(ids, (100.0, -40.0, 60.0)):
        ops_db.update_trade_exit(tid, net_pnl=pnl, exit_reason="TARGET_HIT")
    # The performance router reads trades from its own module-global `db` and
    # snapshots from module-global `db_manager` (separate attrs) — repoint BOTH
    # at this test's DB before hitting the endpoint.
    from backend.api.routers import performance as perf_mod
    original_db = perf_mod.db
    original_dbm = perf_mod.db_manager
    perf_mod.db = ops_db
    perf_mod.db_manager = ops_db
    try:
        r = _client().get("/api/performance")
    finally:
        perf_mod.db = original_db
        perf_mod.db_manager = original_dbm
    assert r.status_code == 200
    m = r.json()["metrics"]
    assert m["total_trades"] == 3
    assert m["net_profit"] == 120.0
    assert m["profit_factor"] == 4.0  # router: gross_profit 160 / gross_loss 40


def test_legacy_db_without_table_recovers(ops_db):
    """A DB where the table is missing must self-heal instead of raising."""
    ops_db._connect().execute("DROP TABLE performance_snapshots")
    ops_db._connect().commit()
    rows = ops_db.list_performance_snapshots()
    assert rows == []
    assert ops_db.insert_performance_snapshot(date="2026-09-28") > 0


# ── F. Health honesty ──────────────────────────────────────────────────
def test_reconciliation_never_checked_stays_never_checked(ops_db):
    from backend.copilot.full_context import build_full_context
    ctx = build_full_context(app_state=None, include_errors=False)
    rec = ctx["reconciliation"]
    assert rec["state"] == "NEVER_CHECKED"
    assert rec["honest_note"]  # explicitly says NEVER_CHECKED is not HEALTHY


def test_websocket_not_streaming_is_not_healthy():
    from backend.copilot.full_context import _websocket
    from types import SimpleNamespace

    class FakeWS:
        def status_report(self):
            return {"state": "connected", "streaming": False,
                    "market_data_status": "STALE", "last_tick_age_seconds": 120.0}
    rep = _websocket(SimpleNamespace(ws_client=FakeWS(), engine=None, scanner=None))
    assert rep["state"] == "connected"
    assert "NOT healthy" in rep["health_interpretation"]


def test_websocket_streaming_is_healthy():
    from backend.copilot.full_context import _websocket
    from types import SimpleNamespace

    class FakeWS:
        def status_report(self):
            return {"state": "streaming", "streaming": True,
                    "market_data_status": "LIVE", "last_tick_age_seconds": 3.0}
    rep = _websocket(SimpleNamespace(ws_client=FakeWS(), engine=None, scanner=None))
    assert "STREAMING = healthy" in rep["health_interpretation"]


def test_scanner_honest_when_no_state(ops_db):
    from backend.copilot.full_context import build_full_context
    ctx = build_full_context(app_state=None, include_errors=False)
    assert ctx["scanner"]["available"] is False
    assert ctx["scanner"]["reason"]
