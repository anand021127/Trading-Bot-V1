"""Tests for API router registration and settings loading."""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.api.main import app


def test_overview_router_is_available() -> None:
    client = TestClient(app)
    response = client.get("/api/overview")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
