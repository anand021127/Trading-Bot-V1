"""Tests for the backend API entrypoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.api.main import app


def test_health_route() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    json_data = response.json()
    assert json_data["status"] == "ok"
    assert json_data["mode"] == "paper"
    assert "timestamp" in json_data


def test_root_route() -> None:
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_api_health_route() -> None:
    client = TestClient(app)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
