"""Tests for the settings router."""

import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.api.main import app


def test_settings_route_returns_app_info() -> None:
    client = TestClient(app)
    response = client.get("/api/settings/")

    assert response.status_code == 200
    json_data = response.json()
    assert json_data["mode"] in ("paper", "live")
    assert json_data["broker_base_url"].startswith("https://")
    assert "frontend_url" in json_data
    assert "notifications" in json_data
    assert "capital" in json_data
    assert "risk" in json_data
    assert "cash_buffer" in json_data["capital"]
    assert "pause_after_losses_minutes" in json_data["risk"]
    assert "indicators" in json_data
    assert "strategy" in json_data


def test_yaml_defaults_has_broker_fields() -> None:
    """Regression: Settings must expose broker.base_url / websocket_url."""
    from backend.config.settings import load_settings
    s = load_settings()
    assert hasattr(s, "broker")
    assert s.broker.base_url.startswith("https://")
    assert "wss://" in s.broker.websocket_url or "ws://" in s.broker.websocket_url
    assert hasattr(s.capital, "cash_buffer")
    assert hasattr(s, "indicators")
    assert hasattr(s.risk, "pause_after_losses_minutes")


def test_broker_status_does_not_crash_without_token() -> None:
    client = TestClient(app)
    response = client.get("/api/settings/broker-status")
    assert response.status_code == 200
    body = response.json()
    assert "overall" in body
    assert "websocket_url" in body
    assert body["websocket_url"]


def test_disconnect_token_clears_env_and_stops_websocket() -> None:
    client = TestClient(app)
    with patch.dict(os.environ, {"UPSTOX_ACCESS_TOKEN": "some-leftover-token"}):
        response = client.post("/api/settings/disconnect-token")
        assert response.status_code == 200
        assert response.json()["status"] == "disconnected"
        assert os.environ.get("UPSTOX_ACCESS_TOKEN", "") == ""


def test_disconnect_token_actually_clears_the_db_token_too() -> None:
    """Regression test: a token saved in an earlier session was being
    silently reloaded from the DB on every request even after clearing the
    env var — disconnect must clear the persisted copy too."""
    from backend.api.routers.settings import _db
    _db.save_token("leftover-from-earlier-session")
    client = TestClient(app)
    client.post("/api/settings/disconnect-token")
    assert not _db.load_token()


def test_token_callback_restarts_websocket_client_with_fresh_token() -> None:
    """Regression test: the WebSocket client only started once at boot. If
    a token was saved to the DB afterward (e.g. via this OAuth callback),
    the socket stayed stuck in auth_failed forever even though REST calls
    worked fine. The callback must now restart it."""
    from backend.api.routers.settings import _restart_websocket_client
    import backend.api.main as main_mod

    with patch("backend.broker.websocket_client.UpstoxWebSocketClient") as MockClient:
        instance = MockClient.return_value
        _restart_websocket_client("fresh-token-value")
        MockClient.assert_called_once()
        _, kwargs = MockClient.call_args
        assert kwargs["access_token"] == "fresh-token-value"
        instance.start.assert_called_once()
        assert main_mod.app.state.ws_client is instance
