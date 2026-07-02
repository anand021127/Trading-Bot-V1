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
