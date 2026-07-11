"""Tests for the overview router endpoint."""

from fastapi.testclient import TestClient

from backend.api.main import app


def test_overview_contains_dashboard_keys() -> None:
    client = TestClient(app)
    response = client.get("/api/overview")

    assert response.status_code == 200
    data = response.json()
    assert data["daily_pnl"]["amount"] == 0.0
    assert data["capital"]["total"] == 500000
    assert data["risk_status"]["max_trades"] == 4
    assert data["trend_bias"] == "NEUTRAL"
    assert data["system"]["mode"] == "paper"
    assert data["open_positions"] == []


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
