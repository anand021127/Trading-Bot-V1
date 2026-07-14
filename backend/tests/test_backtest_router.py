"""Tests for the backtest router — async/background job redesign (fixes
the 'timeout of 30000ms exceeded' bug) + no-synthetic-data guarantee
(items #6 + #10)."""
from __future__ import annotations

import os
import time
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.api.main import app


def test_run_backtest_without_token_returns_explicit_error_not_fake_data() -> None:
    client = TestClient(app)
    with patch.dict(os.environ, {"UPSTOX_ACCESS_TOKEN": ""}, clear=False), \
         patch("backend.api.routers.backtest.db.load_token", return_value=""):
        response = client.post(
            "/api/backtest/run",
            json={"start_date": "2025-01-01", "end_date": "2025-12-31"},
        )
    assert response.status_code == 400
    assert "token" in response.json()["detail"].lower()


def test_run_backtest_returns_task_id_immediately() -> None:
    """This is the actual fix: /run must return almost instantly with a
    task_id, never block on the real fetch+simulation work."""
    client = TestClient(app)
    with patch("backend.api.routers.backtest.db.load_token",
               return_value="fake-token-value-1234567890"):
        start = time.monotonic()
        response = client.post(
            "/api/backtest/run",
            json={"symbols": ["HDFCBANK"], "start_date": "2025-01-01", "end_date": "2025-12-31"},
        )
        elapsed = time.monotonic() - start

    assert response.status_code == 200
    data = response.json()
    assert "task_id" in data
    assert data["status"] in ("queued", "fetching_data", "running")
    assert elapsed < 5.0  # must return almost immediately, not after the full backtest


def test_status_endpoint_reports_progress_and_result_endpoint_returns_it() -> None:
    def fake_candles(symbol, interval, from_date=None, to_date=None):
        closes = [100.0]
        for i in range(149):
            step = 0.6 if i % 2 == 0 else -0.35
            closes.append(closes[-1] + step)
        return [
            {"open": c - 0.1, "high": c + 0.5, "low": c - 0.5, "close": c,
             "volume": 2500 if i % 20 == 0 else 1000, "timestamp": f"bar-{i:04d}"}
            for i, c in enumerate(closes)
        ]

    with TestClient(app) as client:
        with patch("backend.api.routers.backtest.db.load_token",
                   return_value="fake-token-value-1234567890"), \
             patch("backend.broker.upstox_client.UpstoxClient.get_historical_candles_full_range",
                   side_effect=fake_candles):
            start_response = client.post(
                "/api/backtest/run",
                json={"symbols": ["HDFCBANK"], "start_date": "2025-01-01", "end_date": "2025-12-31"},
            )
            task_id = start_response.json()["task_id"]

            # Poll until completed (background asyncio task — give it a moment).
            completed = False
            for _ in range(50):
                status_response = client.get(f"/api/backtest/status/{task_id}")
                assert status_response.status_code == 200
                if status_response.json()["status"] == "completed":
                    completed = True
                    break
                time.sleep(0.05)

        assert completed, "backtest task never reached 'completed' status"

        result_response = client.get(f"/api/backtest/result/{task_id}")
    assert result_response.status_code == 200
    data = result_response.json()
    assert data["data_source"] == "real_upstox_v3"
    assert data["total_candles_scanned"] == 150
    assert data["trades_taken"] >= 1


def test_unknown_task_id_returns_404_for_status_and_result() -> None:
    client = TestClient(app)
    r1 = client.get("/api/backtest/status/does-not-exist")
    assert r1.status_code == 404
    r2 = client.get("/api/backtest/result/does-not-exist")
    assert r2.status_code == 404


def test_all_symbols_failing_fetch_reports_failed_status_not_fake_success() -> None:
    with TestClient(app) as client:
        with patch("backend.api.routers.backtest.db.load_token",
                   return_value="fake-token-value-1234567890"), \
             patch("backend.broker.upstox_client.UpstoxClient.get_historical_candles_full_range",
                   side_effect=RuntimeError("Upstox API 403: Access forbidden")):
            start_response = client.post(
                "/api/backtest/run",
                json={"symbols": ["HDFCBANK"], "start_date": "2025-01-01", "end_date": "2025-12-31"},
            )
            task_id = start_response.json()["task_id"]

            failed = False
            for _ in range(50):
                status_response = client.get(f"/api/backtest/status/{task_id}")
                if status_response.json()["status"] == "failed":
                    failed = True
                    break
                time.sleep(0.05)

        assert failed
        result_response = client.get(f"/api/backtest/result/{task_id}")
    assert result_response.status_code == 502
    assert "fabricate" in result_response.json()["detail"].lower()
