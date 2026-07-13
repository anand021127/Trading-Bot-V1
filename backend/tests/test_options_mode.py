"""Tests for real NIFTY/SENSEX options-mode support:
  - Auto-selecting the nearest real expiry (never a guessed date).
  - Auto-detecting underlying trend so options mode can run unattended.
  - Lot-size rounding on order execution (a real-money correctness issue —
    Indian F&O can't trade fractional lots).
"""
from __future__ import annotations

import tempfile
import uuid
from datetime import date, timedelta
from unittest.mock import patch

from backend.broker.upstox_client import UpstoxClient, UpstoxAPIError
from backend.database.db_manager import DatabaseManager
from backend.strategy.signal import StrategySignal, SignalType
from backend.strategy.trading_engine import LOT_SIZES, TradingEngine


def _isolated_engine() -> TradingEngine:
    path = f"{tempfile.gettempdir()}/test_options_mode_{uuid.uuid4().hex}.db"
    db = DatabaseManager(db_path=path)
    db.init_db()
    return TradingEngine(db_manager=db)


class TestGetNearestExpiry:
    def test_picks_the_soonest_upcoming_expiry(self) -> None:
        client = UpstoxClient(access_token="tok")
        today = date.today()
        expiries = [
            (today - timedelta(days=3)).isoformat(),   # already passed — must be excluded
            (today + timedelta(days=4)).isoformat(),
            (today + timedelta(days=11)).isoformat(),
        ]
        with patch.object(client, "get_option_expiries", return_value=expiries):
            nearest = client.get_nearest_expiry("NIFTY50")
        assert nearest == (today + timedelta(days=4)).isoformat()

    def test_returns_none_without_fabricating_a_date(self) -> None:
        client = UpstoxClient(access_token="tok")
        with patch.object(client, "get_option_expiries", return_value=[]):
            assert client.get_nearest_expiry("NIFTY50") is None

    def test_returns_none_on_api_failure_not_a_guess(self) -> None:
        client = UpstoxClient(access_token="tok")
        with patch.object(client, "get_option_expiries", side_effect=UpstoxAPIError(500, "boom")):
            assert client.get_nearest_expiry("NIFTY50") is None


class TestDetectUnderlyingTrend:
    def test_bullish_when_ema_conditions_confirm_uptrend(self) -> None:
        engine = _isolated_engine()
        closes = [100.0]
        for i in range(59):
            step = 0.6 if i % 2 == 0 else -0.35
            closes.append(closes[-1] + step)
        candles = [{"open": c - 0.1, "high": c + 0.5, "low": c - 0.5, "close": c,
                    "volume": 1000, "timestamp": f"bar-{i:04d}"} for i, c in enumerate(closes)]
        with patch.object(engine.client, "get_historical_candles", return_value=candles):
            trend = engine.detect_underlying_trend("NIFTY50")
        assert trend == "BULLISH"

    def test_neutral_on_insufficient_data(self) -> None:
        engine = _isolated_engine()
        with patch.object(engine.client, "get_historical_candles", return_value=[]):
            trend = engine.detect_underlying_trend("NIFTY50")
        assert trend == "NEUTRAL"

    def test_neutral_on_fetch_error_not_fabricated(self) -> None:
        engine = _isolated_engine()
        with patch.object(engine.client, "get_historical_candles", side_effect=RuntimeError("boom")):
            trend = engine.detect_underlying_trend("NIFTY50")
        assert trend == "NEUTRAL"


class TestEvaluateOptionPremiumAutoDetection:
    def test_no_expiry_available_gives_explicit_rejection(self) -> None:
        engine = _isolated_engine()
        with patch.object(engine.client, "get_nearest_expiry", return_value=None):
            sig = engine.evaluate_option_premium("NIFTY50")
        assert sig.signal == SignalType.NONE
        assert "expiry" in sig.rejected_reasons[0].lower()

    def test_auto_detects_expiry_and_trend_when_not_supplied(self) -> None:
        engine = _isolated_engine()
        with patch.object(engine.client, "get_nearest_expiry", return_value="2026-02-26") as mock_expiry, \
             patch.object(engine, "detect_underlying_trend", return_value="BULLISH") as mock_trend, \
             patch.object(engine.client, "get_option_chain", return_value=[]), \
             patch.object(engine.client, "get_multiple_quotes", return_value={}):
            sig = engine.evaluate_option_premium("NIFTY50")
        mock_expiry.assert_called_once_with("NIFTY50")
        mock_trend.assert_called_once_with("NIFTY50")
        # empty chain -> contract can't be resolved -> explicit rejection
        assert sig.signal == SignalType.NONE

    def test_explicit_expiry_and_trend_skip_auto_detection(self) -> None:
        engine = _isolated_engine()
        with patch.object(engine.client, "get_nearest_expiry") as mock_expiry, \
             patch.object(engine, "detect_underlying_trend") as mock_trend, \
             patch.object(engine.client, "get_option_chain", return_value=[]), \
             patch.object(engine.client, "get_multiple_quotes", return_value={}):
            engine.evaluate_option_premium("NIFTY50", expiry_date="2026-02-26", underlying_trend="BEARISH")
        mock_expiry.assert_not_called()
        mock_trend.assert_not_called()


class TestLotSizeCompliance:
    def test_option_order_rounds_down_to_whole_lots(self) -> None:
        engine = _isolated_engine()
        sig = StrategySignal(strategy_name="OPTION_PREMIUM", symbol="NIFTY50", signal=SignalType.BUY,
                              confidence=90.0, entry_price=100.0, stop_loss=95.0, target=110.0)
        sig.indicators = {"selected_contract": {"option_type": "CE", "strike": 22000,
                                                 "instrument_key": "NSE_FO|999"}}
        # Force position sizer to want an odd, non-lot-multiple quantity.
        with patch.object(engine.position_sizer, "calculate", return_value=100):
            trade_id = engine.execute_multi_signal(sig)
        assert trade_id is not None
        qty = engine._open_positions["NIFTY50"]["quantity"]
        assert qty % LOT_SIZES["NIFTY50"] == 0
        assert qty > 0

    def test_stock_order_is_not_rounded_to_a_lot_size(self) -> None:
        engine = _isolated_engine()
        sig = StrategySignal(strategy_name="EMA_TREND", symbol="TCS", signal=SignalType.BUY,
                              confidence=90.0, entry_price=100.0, stop_loss=95.0, target=110.0)
        sig.indicators = {"atr": 1.0}  # no selected_contract -> not an option order
        with patch.object(engine.position_sizer, "calculate", return_value=37):
            engine.execute_multi_signal(sig)
        assert engine._open_positions["TCS"]["quantity"] == 37

    def test_position_tracks_contract_instrument_key_for_exit_monitoring(self) -> None:
        engine = _isolated_engine()
        sig = StrategySignal(strategy_name="OPTION_PREMIUM", symbol="SENSEX", signal=SignalType.BUY,
                              confidence=90.0, entry_price=100.0, stop_loss=95.0, target=110.0)
        sig.indicators = {"selected_contract": {"option_type": "PE", "strike": 80000,
                                                 "instrument_key": "NSE_FO|888"}}
        engine.execute_multi_signal(sig)
        assert engine._open_positions["SENSEX"]["contract_instrument_key"] == "NSE_FO|888"

    def test_order_size_above_freeze_limit_is_capped_not_rejected(self) -> None:
        from backend.strategy.trading_engine import FREEZE_QUANTITY_LIMITS
        engine = _isolated_engine()
        sig = StrategySignal(strategy_name="OPTION_PREMIUM", symbol="NIFTY50", signal=SignalType.BUY,
                              confidence=90.0, entry_price=100.0, stop_loss=95.0, target=110.0)
        sig.indicators = {"selected_contract": {"option_type": "CE", "strike": 22000,
                                                 "instrument_key": "NSE_FO|999"}}
        # Position sizer wants way more than the exchange allows per order.
        with patch.object(engine.position_sizer, "calculate", return_value=10_000):
            trade_id = engine.execute_multi_signal(sig)
        assert trade_id is not None
        qty = engine._open_positions["NIFTY50"]["quantity"]
        assert qty <= FREEZE_QUANTITY_LIMITS["NIFTY50"]
        assert qty % LOT_SIZES["NIFTY50"] == 0  # still lot-compliant after capping


class TestExpiryDaySquareOff:
    def test_position_tracks_expiry_date_from_signal(self) -> None:
        engine = _isolated_engine()
        sig = StrategySignal(strategy_name="OPTION_PREMIUM", symbol="NIFTY50", signal=SignalType.BUY,
                              confidence=90.0, entry_price=100.0, stop_loss=95.0, target=110.0)
        sig.indicators = {"selected_contract": {"option_type": "CE", "strike": 22000,
                                                 "instrument_key": "NSE_FO|999"},
                           "expiry_date": "2026-02-26"}
        engine.execute_multi_signal(sig)
        assert engine._open_positions["NIFTY50"]["expiry_date"] == "2026-02-26"

    def test_monitor_closes_option_position_on_its_own_expiry_day(self) -> None:
        import asyncio
        from datetime import date

        engine = _isolated_engine()
        engine._open_positions["NIFTY50"] = {
            "trade_id": "t1", "entry_price": 100.0, "stop_loss": 90.0, "target": 200.0,
            "trailing_stop": 90.0, "strategy_name": "OPTION_PREMIUM", "quantity": 75, "atr": 1.0,
            "side": "long", "entry_time": "2026-01-01T09:30:00+00:00",
            "contract_instrument_key": "NSE_FO|999",
            "expiry_date": date.today().isoformat(),  # expiry is TODAY
        }
        with patch("backend.api.websocket.get_prices_by_symbol", return_value={}), \
             patch.object(engine.client, "get_historical_candles", return_value=[
                 {"open": 100, "high": 105, "low": 98, "close": 102, "volume": 1000, "timestamp": "t1"},
             ]), \
             patch.object(engine, "_close_position") as mock_close:
            asyncio.run(engine._monitor_open_positions())
        mock_close.assert_awaited_once()
        assert mock_close.call_args[0][0] == "NIFTY50"
        assert "EXPIRY_DAY" in mock_close.call_args[0][1]

    def test_monitor_does_not_force_close_when_expiry_is_days_away(self) -> None:
        import asyncio
        from datetime import date, timedelta

        engine = _isolated_engine()
        engine._open_positions["NIFTY50"] = {
            "trade_id": "t1", "entry_price": 100.0, "stop_loss": 90.0, "target": 200.0,
            "trailing_stop": 90.0, "strategy_name": "OPTION_PREMIUM", "quantity": 75, "atr": 1.0,
            "side": "long", "entry_time": "2026-01-01T09:30:00+00:00",
            "contract_instrument_key": "NSE_FO|999",
            "expiry_date": (date.today() + timedelta(days=3)).isoformat(),
        }
        with patch("backend.api.websocket.get_prices_by_symbol", return_value={}), \
             patch.object(engine.client, "get_historical_candles", return_value=[
                 {"open": 100, "high": 105, "low": 98, "close": 102, "volume": 1000, "timestamp": "t1"},
             ]), \
             patch.object(engine, "_close_position") as mock_close:
            asyncio.run(engine._monitor_open_positions())
        mock_close.assert_not_awaited()
