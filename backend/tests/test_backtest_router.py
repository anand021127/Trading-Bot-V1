"""Tests for the backtest router (item #6 + #10 — no synthetic-data fallback)."""
from __future__ import annotations

import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.api.main import app


def test_run_backtest_without_token_returns_explicit_error_not_fake_data() -> None:
    """The old router silently fell back to fabricated candles when there
    was no token. The new one must refuse and say so — never return 200
    with fictitious trades."""
    client = TestClient(app)
    with patch.dict(os.environ, {"UPSTOX_ACCESS_TOKEN": ""}, clear=False), \
         patch("backend.api.routers.backtest.db.load_token", return_value=""):
        response = client.post(
            "/api/backtest/run",
            json={"start_date": "2025-01-01", "end_date": "2025-12-31"},
        )
    assert response.status_code == 400
    assert "token" in response.json()["detail"].lower()


def test_run_backtest_with_real_candles_returns_honest_result() -> None:
    """With a token and real (mocked) candle data, the response should come
    from the real BacktestEngine — not a synthetic simulator."""
    client = TestClient(app)

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

    with patch("backend.api.routers.backtest.db.load_token", return_value="fake-token-value-1234567890"), \
         patch("backend.broker.upstox_client.UpstoxClient.get_historical_candles_full_range",
               side_effect=fake_candles):
        response = client.post(
            "/api/backtest/run",
            json={"symbols": ["HDFCBANK"], "start_date": "2025-01-01", "end_date": "2025-12-31"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["data_source"] == "real_upstox_v3"
    assert data["total_candles_scanned"] == 150
    assert data["trades_taken"] >= 1
    assert "rejection_reason_counts" in data
    assert "skipped_symbols" in data
