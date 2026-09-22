"""Regression: backtest API must run the requested strategy with no silent fallback."""
from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("TRADING_MODE", "paper")
os.environ["TRADING_STRATEGY"] = "V8_D_PULLBACK_ATM"
os.environ.setdefault("UPSTOX_ORDER_PRODUCT", "I")
os.environ.setdefault("DATABASE_PATH", "/tmp/bt_strategy_identity.db")


def test_v8d_is_registered():
    from backend.config.strategy_registry import REGISTERED_STRATEGIES, load_strategy
    assert "V8_D_PULLBACK_ATM" in REGISTERED_STRATEGIES
    s = load_strategy("V8_D_PULLBACK_ATM")
    assert s.name == "V8_D_PULLBACK_ATM"


def test_unknown_strategy_does_not_fallback():
    from backend.config.strategy_registry import StrategySelectionError, load_strategy
    try:
        load_strategy("NOT_A_REAL_STRATEGY")
        raise AssertionError("should have failed")
    except StrategySelectionError as exc:
        assert "fallback" in str(exc).lower() or "Unknown" in str(exc)


def test_backtest_api_accepts_v8d_and_records_identity():
    from fastapi.testclient import TestClient
    from backend.api.main import app

    with patch("backend.api.routers.backtest._get_token", return_value="dummy-token"), \
         patch("backend.broker.upstox_client.UpstoxClient"), \
         patch("backend.api.routers.backtest.run_backtest_in_background"):
        client = TestClient(app)
        with patch("backend.backtest.task_manager.run_backtest_in_background"):
            res = client.post(
                "/api/backtest/jobs",
                json={
                    "start_date": "2024-10-01",
                    "end_date": "2024-10-05",
                    "symbols": ["NIFTY50"],
                    "interval": "5minute",
                    "strategies": ["V8_D_PULLBACK_ATM"],
                    "capital": 100000,
                },
            )
    # 202 accepted or 409 if another job exists in-process
    assert res.status_code in (202, 409), res.text
    if res.status_code == 202:
        body = res.json()
        assert body.get("actual_strategy") == "V8_D_PULLBACK_ATM"
        assert "V8_D_PULLBACK_ATM" in (body.get("strategy") or [])


def test_backtest_api_rejects_unknown_strategy():
    from fastapi.testclient import TestClient
    from backend.api.main import app

    with patch("backend.api.routers.backtest._get_token", return_value="dummy-token"), \
         patch("backend.broker.upstox_client.UpstoxClient"):
        client = TestClient(app)
        res = client.post(
            "/api/backtest/jobs",
            json={
                "start_date": "2024-10-01",
                "end_date": "2024-10-05",
                "symbols": ["NIFTY50"],
                "strategies": ["SILENT_FALLBACK_PLEASE"],
            },
        )
    # Unknown strategy must 400 even if another job is queued (409 would mean validation was skipped)
    if res.status_code == 409:
        # Drain active job then retry validation-only request
        from backend.backtest.task_manager import task_manager
        active = task_manager.get_active_task()
        if active:
            task_manager.cancel(active.task_id)
        res = client.post(
            "/api/backtest/jobs",
            json={
                "start_date": "2024-10-01",
                "end_date": "2024-10-05",
                "symbols": ["NIFTY50"],
                "strategies": ["SILENT_FALLBACK_PLEASE"],
            },
        )
    assert res.status_code == 400
    detail = res.json().get("detail")
    assert isinstance(detail, dict)
    assert "SILENT_FALLBACK_PLEASE" in detail.get("unknown", [])
    assert "V8_D_PULLBACK_ATM" in detail.get("known", [])
