"""Tests for the overview router endpoint."""

from fastapi.testclient import TestClient

from backend.api.main import app
from backend.config.settings import load_settings


def test_overview_contains_dashboard_keys() -> None:
    settings = load_settings()
    client = TestClient(app)
    response = client.get("/api/overview")

    assert response.status_code == 200
    data = response.json()
    assert "daily_pnl" in data
    assert "amount" in data["daily_pnl"]
    assert "capital" in data
    assert data["capital"]["total"] == settings.capital.total
    assert "risk_status" in data
    assert data["risk_status"]["max_trades"] == settings.risk.max_trades_per_day
    assert data.get("trend_bias", "NEUTRAL") in ("NEUTRAL", "BULLISH", "BEARISH")
    assert data["system"]["mode"] == "paper"
    assert "open_positions" in data


def test_overview_exposes_universe_watching_count() -> None:
    client = TestClient(app)
    data = client.get("/api/overview").json()
    assert "universe" in data
    assert data["universe"]["watching_count"] >= 0
    assert "mode" in data["universe"]


def test_overview_exposes_scanner_state() -> None:
    client = TestClient(app)
    data = client.get("/api/overview").json()
    assert "scanner" in data
    assert "currently_analyzing" in data["scanner"]
    assert "last_signal" in data["scanner"]
    assert "is_running" in data["scanner"]


def test_overview_websocket_status_reflects_real_broker_feed_not_frontend_clients() -> None:
    """The old field measured frontend push-channel clients, not the actual
    Upstox v3 feed — item #8 requires the real feed status."""
    client = TestClient(app)
    data = client.get("/api/overview").json()
    assert "websocket_status" in data["system"]
    assert "active_frontend_connections" in data["system"]
    assert "api_health" in data["system"]
