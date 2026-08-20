"""Tests for the performance router."""

from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch

from backend.api.main import app
from backend.database.models import PerformanceSnapshot


def test_performance_router_returns_snapshots(monkeypatch) -> None:
    snapshot = PerformanceSnapshot(
        date="2026-07-02",
        net_pnl=150.0,
        trades_count=1,
        win_rate=100.0,
        equity=101500.0,
    )
    mocked_db = MagicMock()
    mocked_db.list_performance_snapshots.return_value = [snapshot]

    with patch("backend.api.routers.performance.db_manager", mocked_db):
        client = TestClient(app)
        response = client.get("/api/performance/")

    assert response.status_code == 200
    assert response.json()["total_snapshots"] == 1
    assert response.json()["performance"][0]["date"] == "2026-07-02"
