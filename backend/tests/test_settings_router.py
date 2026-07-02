"""Tests for the settings router."""

from fastapi.testclient import TestClient

from backend.api.main import app


def test_settings_route_returns_app_info() -> None:
    client = TestClient(app)
    response = client.get("/api/settings/")

    assert response.status_code == 200
    json_data = response.json()
    assert json_data["mode"] == "paper"
    assert json_data["broker_base_url"].startswith("https://")
    assert "frontend_url" in json_data
    assert "notifications" in json_data
