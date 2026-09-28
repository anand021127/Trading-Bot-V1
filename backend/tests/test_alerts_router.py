"""Tests for alerts router endpoints."""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from backend.api.main import app
from backend.config.settings import NotificationSettings, Settings


def _settings_with(**notif_kwargs):
    s = Settings()
    base = {
        "telegram_enabled": False,
    }
    base.update(notif_kwargs)
    s.notifications = NotificationSettings(**base)
    return s


def test_alerts_status_endpoint() -> None:
    client = TestClient(app)
    with patch("backend.api.routers.alerts.load_settings", return_value=_settings_with()):
        response = client.get("/api/alerts/")
    assert response.status_code == 200
    json_data = response.json()
    assert "telegram_enabled" in json_data


def test_alerts_send_test_telegram(monkeypatch) -> None:
    client = TestClient(app)
    with patch("backend.api.routers.alerts.load_settings", return_value=_settings_with(telegram_enabled=True)):
        with patch("backend.api.routers.alerts.TelegramAlerts.send_message") as mocked:
            mocked.return_value = {"ok": True}
            response = client.post("/api/alerts/test?channel=telegram")
    assert response.status_code == 200
    assert response.json()["channel"] == "telegram"


def test_alerts_email_channel_removed() -> None:
    """Email alerts were removed from the product entirely; the endpoint must
    refuse the channel instead of attempting any SMTP operation."""
    client = TestClient(app)
    response = client.post("/api/alerts/test?channel=email")
    assert response.status_code == 400
    assert response.json()["detail"] == "unsupported channel"


def test_alerts_test_channel_invalid() -> None:
    client = TestClient(app)
    response = client.post("/api/alerts/test?channel=unsupported")
    assert response.status_code == 400
