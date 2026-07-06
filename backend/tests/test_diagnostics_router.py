"""Tests for diagnostics endpoints."""

from fastapi.testclient import TestClient

from backend.api.main import app


def test_diagnostics_endpoint_returns_ok() -> None:
    client = TestClient(app)
    response = client.get("/api/diagnostics/")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["mode"] == "paper"


def test_diagnostics_env_endpoint_returns_keys() -> None:
    client = TestClient(app)
    response = client.get("/api/diagnostics/env")

    assert response.status_code == 200
    assert "mode" in response.json()
    assert "frontend_url" in response.json()
