"""Tests for alerts router endpoints."""

from fastapi.testclient import TestClient
from unittest.mock import patch

from backend.api.main import app


def test_alerts_status_endpoint() -> None:
    client = TestClient(app)
    response = client.get("/api/alerts/")

    assert response.status_code == 200
    json_data = response.json()
    assert "email_enabled" in json_data
    assert "telegram_enabled" in json_data


def test_alerts_send_test_telegram(monkeypatch) -> None:
    client = TestClient(app)
    with patch("backend.api.routers.alerts.TelegramAlerts.send_message") as mocked:
        mocked.return_value = {"ok": True}
        response = client.post("/api/alerts/test?channel=telegram")

    assert response.status_code == 200
    assert response.json()["channel"] == "telegram"


def test_alerts_send_test_email(monkeypatch) -> None:
    client = TestClient(app)
    with patch("backend.api.routers.alerts.EmailAlerts.send_email") as mocked:
        mocked.return_value = None
        response = client.post("/api/alerts/test?channel=email")

    assert response.status_code == 200
    assert response.json()["channel"] == "email"


def test_alerts_test_channel_invalid() -> None:
    client = TestClient(app)
    response = client.post("/api/alerts/test?channel=unsupported")

    assert response.status_code == 400
