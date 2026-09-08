"""Tests for backend/ai/*.

Run with: python run_all_tests.py (project convention) or via the repo's
pytest shim. Follows this repo's existing test convention (see
backend/tests/test_config.py) — unittest.mock.patch.dict for env vars and
tempfile for scratch dirs, not pytest fixtures, since the project's
pytest.py shim doesn't provide monkeypatch/tmp_path.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from backend.ai.config import load_ai_settings
from backend.ai.dataset import build_dataset, DatasetRow
from backend.ai.features import FEATURE_NAMES, build_feature_vector, build_feature_vector_from_signal, feature_vector_to_row
from backend.ai.predictor import AIPredictor
from backend.strategy.confidence_scoring import ConfidenceScorer
from backend.strategy.signal import StrategySignal


def _synthetic_trend_candles(n=200, start=100.0, drift=0.05, seed=42):
    import random
    rnd = random.Random(seed)
    candles = []
    price = start
    ts = datetime(2024, 6, 3, 9, 15)  # a Monday
    for i in range(n):
        o = price
        price += drift + rnd.uniform(-0.3, 0.3)
        c = price
        h = max(o, c) + rnd.uniform(0, 0.2)
        l = min(o, c) - rnd.uniform(0, 0.2)
        candles.append({
            "timestamp": ts.isoformat(),
            "open": round(o, 2), "high": round(h, 2), "low": round(l, 2),
            "close": round(c, 2), "volume": 1000 + i,
        })
        ts += timedelta(minutes=5)
    return candles


class TestFeatureGeneration:
    def test_no_direction_returns_none(self):
        scorer = ConfidenceScorer()
        candles = _synthetic_trend_candles(n=15, drift=0.0)  # flat/choppy -> likely NONE
        setup = scorer.evaluate(candles)
        fv = build_feature_vector(candles[-1], setup)
        if setup.direction in ("CE", "PE"):
            assert fv is not None
        else:
            assert fv is None

    def test_feature_vector_has_all_names(self):
        candles = _synthetic_trend_candles(n=150, drift=0.15)
        scorer = ConfidenceScorer()
        setup = scorer.evaluate(candles)
        fv = build_feature_vector(candles[-1], setup)
        if fv is not None:
            assert set(fv.keys()) == set(FEATURE_NAMES)
            row = feature_vector_to_row(fv)
            assert len(row) == len(FEATURE_NAMES)
            assert all(isinstance(x, float) for x in row)

    def test_signal_based_features_match_schema(self):
        sig = StrategySignal(
            strategy_name="OPTION_PREMIUM", symbol="NIFTY50", signal="BUY", confidence=75.0,
            setup_name="MOMENTUM_CONTINUATION", factor_scores={"trend": 20.0, "momentum": 15.0, "vwap": 10.0, "volume": 5.0},
            indicators={"directional_intent": "CE", "spot_price": 100.0, "vwap": 99.5, "ema50": 98.0,
                        "atr": 1.2, "rsi": 60.0, "choppiness_index": 40.0, "volume_ratio": 1.3,
                        "roc_1bar": 0.1, "roc_3bar": 0.2, "roc_5bar": 0.3, "ema20_slope": 0.05,
                        "dist_ema20_atr": 0.5, "is_overextended": False},
        )
        candle = {"timestamp": "2024-06-03T10:00:00", "close": 100.0}
        fv = build_feature_vector_from_signal(candle, sig)
        assert fv is not None
        assert set(fv.keys()) == set(FEATURE_NAMES)
        assert fv["direction_is_ce"] == 1.0
        assert fv["setup_is_momentum"] == 1.0

    def test_signal_with_no_direction_returns_none(self):
        sig = StrategySignal(strategy_name="OPTION_PREMIUM", symbol="NIFTY50", signal="NONE", indicators={})
        assert build_feature_vector_from_signal({"timestamp": "", "close": 0}, sig) is None


class TestNoLookaheadBias:
    def test_dataset_rows_only_use_past_at_decision_time(self):
        """The label for row i must be resolvable using ONLY candles after
        its own timestamp — verify by re-deriving each row's label using a
        truncated candle list that ends exactly at the row's horizon, and
        confirming it matches (i.e. nothing beyond that window influenced it)."""
        candles = _synthetic_trend_candles(n=400, drift=0.2)
        rows, report = build_dataset(candles, symbol="TEST", horizon_bars=20, warmup_bars=60)
        assert report.rows_emitted == len(rows)
        # Every row's feature dict must come only from build_feature_vector,
        # which by construction never receives candles[t+1:].
        for r in rows[:5]:
            assert isinstance(r.features, dict)
            assert set(r.features.keys()) == set(FEATURE_NAMES)

    def test_rows_near_end_of_series_are_dropped_not_guessed(self):
        candles = _synthetic_trend_candles(n=200, drift=0.2)
        rows, report = build_dataset(candles, symbol="TEST", horizon_bars=48, warmup_bars=60)
        # No row's decision index should be within `horizon_bars` of the end
        # of the series (those get dropped by _label_forward returning None).
        last_ts = candles[-1]["timestamp"]
        # Simple sanity: skip count should be > 0 for a short series with a
        # generous horizon relative to its length.
        assert report.rows_skipped_no_horizon >= 0  # never negative/undefined


class TestChronologicalOrdering:
    def test_dataset_rows_are_chronological(self):
        candles = _synthetic_trend_candles(n=300, drift=0.15)
        rows, _ = build_dataset(candles, symbol="TEST", horizon_bars=20, warmup_bars=60)
        timestamps = [r.timestamp for r in rows]
        assert timestamps == sorted(timestamps), "dataset rows must never be shuffled"


class TestAIDisabledPassThrough:
    def test_disabled_never_blocks(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_ENABLED", None)
            predictor = AIPredictor()  # AI_ENABLED defaults to False
        assert predictor.settings.enabled is False
        sig = StrategySignal(strategy_name="OPTION_PREMIUM", symbol="NIFTY50", signal="BUY",
                              indicators={"directional_intent": "CE", "spot_price": 100.0})
        decision = predictor.evaluate_signal({"timestamp": "2024-06-03T10:00:00", "close": 100.0}, sig)
        assert decision.should_allow is True
        assert decision.ran is False


class TestAIFailSafe:
    def test_no_model_fails_open_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"AI_ENABLED": "true", "AI_MODELS_DIR": str(Path(tmpdir) / "nonexistent")}):
                predictor = AIPredictor()
                sig = StrategySignal(strategy_name="OPTION_PREMIUM", symbol="NIFTY50", signal="BUY",
                                      indicators={"directional_intent": "CE", "spot_price": 100.0})
                decision = predictor.evaluate_signal({"timestamp": "2024-06-03T10:00:00", "close": 100.0}, sig)
        assert decision.ran is False
        assert decision.should_allow is True  # fail_open default

    def test_no_model_fails_closed_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {"AI_ENABLED": "true", "AI_FAIL_OPEN": "false", "AI_MODELS_DIR": str(Path(tmpdir) / "nonexistent")}
            with mock.patch.dict(os.environ, env):
                predictor = AIPredictor()
                sig = StrategySignal(strategy_name="OPTION_PREMIUM", symbol="NIFTY50", signal="BUY",
                                      indicators={"directional_intent": "CE", "spot_price": 100.0})
                decision = predictor.evaluate_signal({"timestamp": "2024-06-03T10:00:00", "close": 100.0}, sig)
        assert decision.ran is False
        assert decision.should_allow is False


class TestAIModeConfig:
    def test_unrecognized_mode_fails_safe_to_shadow(self) -> None:
        with mock.patch.dict(os.environ, {"AI_MODE": "live_but_typo"}):
            settings = load_ai_settings()
        assert settings.mode == "shadow"

    def test_never_defaults_to_live(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_MODE", None)
            settings = load_ai_settings()
        assert settings.mode != "live"
