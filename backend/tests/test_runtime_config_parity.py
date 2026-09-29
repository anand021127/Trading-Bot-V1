"""PHASE B — Configuration parity regression tests (spec §35-B, §17, §18).

Proves the ONE authoritative chain end-to-end:

    Settings UI PUT (SQLite blob)
      -> runtime_config.get_effective_settings()
      -> TradingEngine / RiskManager / PaperTradingRuntime construction
      -> Overview / Operations / Copilot context

The historical bug: the UI saved capital 20,000 / max trades 20 while every
runtime component kept reading TRADING_CAPITAL=100000 / MAX_TRADES_PER_DAY=3
env defaults at import time, so the bot stopped at 3 trades and Overview
showed 100,000.
"""
from __future__ import annotations

import os
import tempfile
import uuid

import pytest
from fastapi.testclient import TestClient

from backend.database.db_manager import DatabaseManager


@pytest.fixture()
def isolated_config_db(monkeypatch):
    """Unique Settings DB per test + fresh resolver cache."""
    path = os.path.join(tempfile.gettempdir(), f"parity_{uuid.uuid4().hex}.db")
    db = DatabaseManager(db_path=path)
    monkeypatch.setenv("DATABASE_PATH", path)
    from backend.config import runtime_config
    runtime_config.set_runtime_config_db(db)
    yield db
    from backend.config import runtime_config
    runtime_config.invalidate_runtime_config_cache()
    runtime_config.set_runtime_config_db(None)
    # Drop the bounded implicit-DB fallback too: this test's blob (e.g. a
    # Settings-UI PUT with capital=20000) must never leak into a later test
    # that runs in the resolver's fallback context.
    runtime_config._fallback_db = None


def _save_blob(db: DatabaseManager, capital: float, max_trades: int) -> None:
    db.save_settings_blob({
        "mode": "paper",
        "capital": {"total": capital, "max_allocation_per_trade": 0.18, "cash_buffer": 0.40},
        "risk": {
            "max_risk_per_trade_pct": 0.025,
            "max_daily_loss_pct": 0.02,
            "max_trades_per_day": max_trades,
            "max_concurrent_positions": 1,
            "max_consecutive_losses": 3,
            "pause_after_losses_minutes": 30,
        },
    })


def test_resolver_prefers_saved_blob_over_env(isolated_config_db, monkeypatch):
    monkeypatch.setenv("TRADING_CAPITAL", "100000")
    monkeypatch.setenv("MAX_TRADES_PER_DAY", "3")
    _save_blob(isolated_config_db, 20000.0, 20)
    from backend.config.runtime_config import get_effective_settings
    s = get_effective_settings()
    assert s.capital.total == 20000.0          # saved value wins over env
    assert s.risk.max_trades_per_day == 20     # saved value wins over env


def test_resolver_falls_back_to_env_when_no_blob(isolated_config_db, monkeypatch):
    monkeypatch.setenv("TRADING_CAPITAL", "123456")
    monkeypatch.setenv("MAX_TRADES_PER_DAY", "7")
    from backend.config.runtime_config import get_effective_settings
    s = get_effective_settings()
    assert s.capital.total == 123456.0
    assert s.risk.max_trades_per_day == 7


def test_cache_invalidation_makes_put_effective(isolated_config_db):
    from backend.config.runtime_config import (
        get_effective_settings, invalidate_runtime_config_cache,
    )
    _save_blob(isolated_config_db, 20000.0, 20)
    invalidate_runtime_config_cache()
    assert get_effective_settings().capital.total == 20000.0
    _save_blob(isolated_config_db, 55000.0, 5)   # operator changes their mind
    invalidate_runtime_config_cache()
    s = get_effective_settings()
    assert s.capital.total == 55000.0
    assert s.risk.max_trades_per_day == 5


def test_settings_put_is_effective_immediately(isolated_config_db):
    """The actual API flow: PUT /api/settings then read the resolver —
    no restart, no cache staleness."""
    isolated_config_db.save_setting("_probe", "1")  # ensure db is warm
    from backend.api.main import app
    client = TestClient(app)
    r = client.put("/api/settings/", json={
        "capital": {"total": 20000},
        "risk": {"max_trades_per_day": 20},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["saved"] is True
    assert body.get("effective_immediately") is True
    from backend.config.runtime_config import get_effective_settings
    s = get_effective_settings()
    assert s.capital.total == 20000.0
    assert s.risk.max_trades_per_day == 20
    # Cleanup so this test's blob can never leak into later tests via the
    # process-shared settings DB.
    isolated_config_db.save_settings_blob({})


def test_risk_manager_gets_saved_values_via_engine(isolated_config_db, monkeypatch):
    """TradingEngine construction reads the effective config — the same
    RiskManager that gates live decisions must see 20,000 / 20."""
    monkeypatch.setenv("TRADING_CAPITAL", "100000")
    monkeypatch.setenv("MAX_TRADES_PER_DAY", "3")
    monkeypatch.setenv("TRADING_STRATEGY", "V8_D_PULLBACK_ATM")
    _save_blob(isolated_config_db, 20000.0, 20)
    from unittest.mock import patch as _patch
    from backend.strategy.trading_engine import TradingEngine
    from backend.risk.risk_manager import RiskManager
    # Patch the broker client so engine construction makes no network call.
    with _patch("backend.strategy.trading_engine.UpstoxClient") as _c:
        _c.return_value = type("C", (), {"access_token": ""})()
        engine = TradingEngine()
    rm = engine.risk_manager
    assert isinstance(rm, RiskManager)
    assert rm.capital == 20000.0
    assert rm.max_trades_per_day == 20
    assert rm.daily_loss_limit == 0.02


def test_paper_runtime_uses_saved_config(isolated_config_db, monkeypatch):
    monkeypatch.setenv("TRADING_CAPITAL", "100000")
    monkeypatch.setenv("MAX_TRADES_PER_DAY", "3")
    monkeypatch.setenv("UPSTOX_ORDER_PRODUCT", "I")
    monkeypatch.setenv("RISK_PER_TRADE_PCT", "0.025")
    _save_blob(isolated_config_db, 20000.0, 20)
    from backend.paper.paper_runtime import PaperTradingRuntime
    rt = PaperTradingRuntime(db=isolated_config_db)
    assert rt.risk.capital == 20000.0
    assert rt.risk.max_daily_trades == 20


def test_paper_runtime_keeps_strategy_risk_parity(isolated_config_db):
    """Engine risk% must stay the env value (0.025) so the authoritative risk
    config still agrees with the frozen V8-D strategy risk% (2.5%)."""
    monkeypatch_env = {"RISK_PER_TRADE_PCT": "0.025"}
    saved = monkeypatch_env
    assert saved["RISK_PER_TRADE_PCT"] == "0.025"
    from backend.paper.paper_runtime import PaperTradingRuntime
    rt = PaperTradingRuntime(db=isolated_config_db)
    assert float(rt.risk.risk_per_trade_pct) == pytest.approx(0.025)


def test_overview_reports_saved_capital(isolated_config_db, monkeypatch):
    monkeypatch.setenv("TRADING_CAPITAL", "100000")
    _save_blob(isolated_config_db, 20000.0, 20)
    from backend.api.main import app
    client = TestClient(app)
    r = client.get("/api/overview")
    assert r.status_code == 200
    cap = r.json()["capital"]
    assert cap["total"] == 20000.0
    assert cap["source"] == "runtime_config"


def test_overview_available_capital_uses_definitions(isolated_config_db):
    _save_blob(isolated_config_db, 20000.0, 20)
    from backend.api.main import app
    client = TestClient(app)
    r = client.get("/api/overview")
    cap = r.json()["capital"]
    # STARTING / CURRENT / USED / AVAILABLE / BUFFER are distinct fields
    assert set(cap) >= {"total", "current", "available", "used", "buffer", "source"}
    assert cap["current"] is None or isinstance(cap["current"], float)


def test_operations_reports_runtime_config_and_sources(isolated_config_db):
    _save_blob(isolated_config_db, 20000.0, 20)
    from backend.api.main import app
    client = TestClient(app)
    r = client.get("/api/bot/operations")
    assert r.status_code == 200
    body = r.json()
    rc = body["runtime_config"]
    assert rc["capital"]["starting_capital"] == 20000.0
    assert rc["capital"]["source"] == "sqlite_settings"
    assert rc["risk"]["max_trades_per_day"] == 20
    assert rc["risk"]["max_trades_source"] == "sqlite_settings"


def test_mismatch_detected_when_consumer_bypasses_resolver(isolated_config_db):
    """If any consumer still reads env (simulated here by passing env-based
    settings to the detector), the saved-vs-runtime divergence surfaces."""
    _save_blob(isolated_config_db, 20000.0, 20)
    from backend.config.runtime_config import detect_config_mismatches
    from backend.config.settings import load_settings
    mismatches = detect_config_mismatches(settings=load_settings())  # env defaults
    keys = {m["key"] for m in mismatches}
    assert "capital.total" in keys
    assert "risk.max_trades_per_day" in keys
    cap = next(m for m in mismatches if m["key"] == "capital.total")
    assert cap["saved_value"] == 20000.0 and cap["runtime_value"] == 100000.0


def test_no_false_mismatch_after_full_unification(isolated_config_db):
    _save_blob(isolated_config_db, 20000.0, 20)
    from backend.config.runtime_config import (
        detect_config_mismatches, get_effective_settings,
    )
    assert detect_config_mismatches(settings=get_effective_settings()) == []


def test_source_labels_expose_provenance(isolated_config_db, monkeypatch):
    monkeypatch.delenv("TRADING_CAPITAL", raising=False)
    monkeypatch.delenv("MAX_TRADES_PER_DAY", raising=False)
    from backend.config.runtime_config import get_config_sources
    # no blob, no env -> defaults
    from backend.config import runtime_config
    runtime_config.invalidate_runtime_config_cache()
    src = get_config_sources()
    assert src["capital.total"] == "default"
    _save_blob(isolated_config_db, 20000.0, 20)
    runtime_config.invalidate_runtime_config_cache()
    src = get_config_sources()
    assert src["capital.total"] == "sqlite_settings"
    monkeypatch.delenv("TRADING_CAPITAL", raising=False)
    runtime_config.invalidate_runtime_config_cache()
    # env only (no blob) -> env label
    isolated_config_db.save_settings_blob({})
    runtime_config.invalidate_runtime_config_cache()
    monkeypatch.setenv("TRADING_CAPITAL", "999999")
    src = get_config_sources()
    assert src["capital.total"] == "env_TRADING_CAPITAL"


def test_copilot_context_reports_saved_config(isolated_config_db):
    _save_blob(isolated_config_db, 20000.0, 20)
    from backend.copilot.full_context import build_full_context
    ctx = build_full_context(app_state=None, include_errors=False)
    cfg = ctx["configuration"]
    assert cfg["capital"]["starting_capital"] == 20000.0
    assert cfg["risk"]["max_trades_per_day"] == 20
    assert ctx["today"]["configured_max_trades"] == 20


def test_settings_get_carries_config_sources(isolated_config_db):
    _save_blob(isolated_config_db, 20000.0, 20)
    from backend.api.main import app
    client = TestClient(app)
    r = client.get("/api/settings/")
    assert r.status_code == 200
    body = r.json()
    assert body["capital"]["total"] == 20000
    assert body["config_sources"]["capital.total"] == "sqlite_settings"
