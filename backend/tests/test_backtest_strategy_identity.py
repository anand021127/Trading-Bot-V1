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


def test_v8d_request_engine_excludes_option_premium():
    from backend.config.strategy_registry import load_strategies
    from backend.strategy.strategy_engine import MultiStrategyEngine

    loaded = load_strategies(["V8_D_PULLBACK_ATM"])
    eng = MultiStrategyEngine(strategies=loaded)
    assert eng.enabled_names() == ["V8_D_PULLBACK_ATM"]
    assert "OPTION_PREMIUM" not in eng.enabled_names()


def test_empty_load_strategies_refuses_fallback():
    from backend.config.strategy_registry import StrategySelectionError, load_strategies

    try:
        load_strategies([])
        raise AssertionError("should have failed")
    except StrategySelectionError as exc:
        assert "OPTION_PREMIUM" in str(exc) or "fallback" in str(exc).lower()


def test_v8d_run_never_loads_option_premium():
    from backend.backtest.engine import BacktestEngine

    engine = BacktestEngine(min_candles_required=5)
    candles = {
        "NIFTY50": [
            {
                "open": 24000 + i,
                "high": 24010 + i,
                "low": 23990 + i,
                "close": 24000 + i,
                "volume": 1000,
                "timestamp": f"2024-01-02T10:{i:02d}:00+05:30",
            }
            for i in range(80)
        ]
    }
    res = engine.run(
        symbol_candles=candles,
        strategy_names=["V8_D_PULLBACK_ATM"],
        require_real_options=False,
    )
    assert res.strategy_names == ["V8_D_PULLBACK_ATM"]
    assert engine.strategy_engine is not None
    assert "OPTION_PREMIUM" not in engine.strategy_engine.enabled_names()
    assert "V8_D_PULLBACK_ATM" in engine.strategy_engine.enabled_names()
    for t in getattr(res, "trades", []) or []:
        strat = t.strategy if hasattr(t, "strategy") else (t.get("strategy") if isinstance(t, dict) else None)
        assert strat == "V8_D_PULLBACK_ATM"


def test_mixed_v8d_and_option_premium_refused():
    """Pre-loaded mixed engine must rebind to ONLY V8_D when that is requested."""
    from backend.backtest.engine import BacktestEngine
    from backend.strategy.strategies.option_premium import OptionPremiumStrategy
    from backend.strategy.strategies.v8d_strategy import V8DStrategy
    from backend.strategy.strategy_engine import MultiStrategyEngine

    engine = BacktestEngine(
        strategy_engine=MultiStrategyEngine([V8DStrategy(), OptionPremiumStrategy()]),
        min_candles_required=5,
    )
    candles = {
        "NIFTY50": [
            {
                "open": 100 + i,
                "high": 101 + i,
                "low": 99 + i,
                "close": 100 + i,
                "volume": 1000,
                "timestamp": f"2024-01-02T10:{i:02d}:00+05:30",
            }
            for i in range(80)
        ]
    }
    res = engine.run(symbol_candles=candles, strategy_names=["V8_D_PULLBACK_ATM"], require_real_options=False)
    assert res.strategy_names == ["V8_D_PULLBACK_ATM"]
    assert engine.strategy_engine.enabled_names() == ["V8_D_PULLBACK_ATM"]
    assert "OPTION_PREMIUM" not in engine.strategy_engine.enabled_names()
    for t in getattr(res, "trades", []) or []:
        strat = t.strategy if hasattr(t, "strategy") else (t.get("strategy") if isinstance(t, dict) else None)
        assert strat == "V8_D_PULLBACK_ATM"


def test_preloaded_option_premium_engine_rebounds_to_v8d():
    """Engine pre-loaded with only OPTION_PREMIUM must rebind when V8_D is requested."""
    from backend.backtest.engine import BacktestEngine
    from backend.strategy.strategies.option_premium import OptionPremiumStrategy
    from backend.strategy.strategy_engine import MultiStrategyEngine

    engine = BacktestEngine(
        strategy_engine=MultiStrategyEngine([OptionPremiumStrategy()]),
        min_candles_required=5,
    )
    candles = {
        "NIFTY50": [
            {
                "open": 24000 + i,
                "high": 24010 + i,
                "low": 23990 + i,
                "close": 24000 + i,
                "volume": 1000,
                "timestamp": f"2024-01-02T10:{i:02d}:00+05:30",
            }
            for i in range(80)
        ]
    }
    res = engine.run(symbol_candles=candles, strategy_names=["V8_D_PULLBACK_ATM"], require_real_options=False)
    assert res.strategy_names == ["V8_D_PULLBACK_ATM"]
    assert engine.strategy_engine.enabled_names() == ["V8_D_PULLBACK_ATM"]
    assert "OPTION_PREMIUM" not in engine.strategy_engine.enabled_names()
    for t in getattr(res, "trades", []) or []:
        strat = t.strategy if hasattr(t, "strategy") else (t.get("strategy") if isinstance(t, dict) else None)
        assert strat == "V8_D_PULLBACK_ATM"


def test_default_multi_strategy_engine_is_empty_not_option_premium():
    from backend.strategy.strategy_engine import MultiStrategyEngine

    eng = MultiStrategyEngine()
    assert eng.enabled_names() == []
    assert "OPTION_PREMIUM" not in eng.enabled_names()


def test_validity_status_zero_trades_vs_invalid_data():
    """Complete coverage + 0 trades → ZERO_TRADES; incomplete → INVALID_DATA."""
    from backend.backtest.engine import BacktestEngine, BacktestResult

    # Spot-only path with enough candles: coverage complete, 0 trades → ZERO_TRADES or INCONCLUSIVE
    engine = BacktestEngine(min_candles_required=5)
    candles = {
        "NIFTY50": [
            {
                "open": 24000 + i,
                "high": 24010 + i,
                "low": 23990 + i,
                "close": 24000 + i,
                "volume": 1000,
                "timestamp": f"2024-01-02T10:{i:02d}:00+05:30",
            }
            for i in range(80)
        ]
    }
    res = engine.run(
        symbol_candles=candles,
        strategy_names=["V8_D_PULLBACK_ATM"],
        require_real_options=False,
        requested_start_date="2024-01-02",
        requested_end_date="2024-01-02",
        min_coverage_pct=50.0,
    )
    assert res.strategy_names == ["V8_D_PULLBACK_ATM"]
    assert res.validity_status in ("ZERO_TRADES", "INCONCLUSIVE", "VALID")
    if res.trades_taken == 0 and res.signals_generated == 0:
        assert res.validity_status == "INCONCLUSIVE"
    elif res.trades_taken == 0:
        assert res.validity_status == "ZERO_TRADES"
    else:
        assert res.validity_status == "VALID"

    # Incomplete requested window → INVALID_DATA
    res2 = engine.run(
        symbol_candles=candles,
        strategy_names=["V8_D_PULLBACK_ATM"],
        require_real_options=False,
        requested_start_date="2024-01-01",
        requested_end_date="2024-12-31",
        min_coverage_pct=80.0,
    )
    assert res2.validity_status == "INVALID_DATA"
    assert any("coverage" in r.lower() or "%" in r for r in (res2.validity_reasons or []))
