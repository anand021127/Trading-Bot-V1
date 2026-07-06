"""Tests for the backtest router."""

from fastapi.testclient import TestClient

from backend.api.main import app


def test_run_backtest_returns_summary() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/backtest/run",
        json={"start_date": "2025-01-01", "end_date": "2025-12-31", "commission_pct": 0.0005},
    )

    assert response.status_code == 200
    json_data = response.json()
    assert json_data["status"] == "completed"
    assert json_data["summary"]["start_date"] == "2025-01-01"
    assert json_data["summary"]["end_date"] == "2025-12-31"
