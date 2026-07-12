"""Tests for the live trading loop fixes:
  - _monitor_open_positions now actually enforces stop/target/trailing-stop
    intraday (previously the only exit path was the 14:45 force-exit).
  - execute_multi_signal wires the new multi-strategy engine into real
    order execution.
  - The "daily floor trade" only ever fires on a freshly-recomputed, real
    signal — never a fabricated one.
"""
from __future__ import annotations

import asyncio
import tempfile
import uuid
from unittest.mock import patch

from backend.database.db_manager import DatabaseManager
from backend.strategy.signal import StrategySignal, SignalType
from backend.strategy.trading_engine import TradingEngine


def _isolated_engine() -> TradingEngine:
    """A TradingEngine backed by a throwaway temp-file DB, so tests that
    exercise real order/position writes never touch the shared default
    database (which other tests and the live app also read from)."""
    path = f"{tempfile.gettempdir()}/test_trading_engine_{uuid.uuid4().hex}.db"
    db = DatabaseManager(db_path=path)
    db.init_db()
    return TradingEngine(db_manager=db)


def _open_position(symbol="HDFCBANK", entry=100.0, stop=95.0, target=110.0, strategy="EMA_TREND"):
    engine = _isolated_engine()
    engine._open_positions[symbol] = {
        "trade_id": "t1", "entry_price": entry, "stop_loss": stop, "target": target,
        "trailing_stop": stop, "strategy_name": strategy, "quantity": 10, "atr": 1.0,
        "side": "long", "entry_time": "2026-01-01T09:30:00+00:00",
    }
    return engine


class TestMonitorOpenPositions:
    def test_stop_loss_hit_closes_the_position(self) -> None:
        engine = _open_position(entry=100.0, stop=95.0, target=110.0)
        with patch("backend.api.websocket.get_prices_by_symbol",
                   return_value={"HDFCBANK": {"ltp": 94.0}}), \
             patch.object(engine, "_close_position") as mock_close:
            asyncio.run(engine._monitor_open_positions())
        mock_close.assert_awaited_once()
        args = mock_close.call_args[0]
        assert args[0] == "HDFCBANK"
        assert args[1] == "STOP_LOSS_HIT"

    def test_target_hit_closes_the_position(self) -> None:
        engine = _open_position(entry=100.0, stop=95.0, target=110.0)
        with patch("backend.api.websocket.get_prices_by_symbol",
                   return_value={"HDFCBANK": {"ltp": 111.0}}), \
             patch.object(engine, "_close_position") as mock_close:
            asyncio.run(engine._monitor_open_positions())
        mock_close.assert_awaited_once_with("HDFCBANK", "TARGET_HIT")

    def test_price_between_stop_and_target_does_not_close(self) -> None:
        engine = _open_position(entry=100.0, stop=95.0, target=110.0)
        with patch("backend.api.websocket.get_prices_by_symbol",
                   return_value={"HDFCBANK": {"ltp": 103.0}}), \
             patch.object(engine.client, "get_historical_candles", return_value=[]), \
             patch.object(engine, "_close_position") as mock_close:
            asyncio.run(engine._monitor_open_positions())
        mock_close.assert_not_awaited()

    def test_no_live_tick_falls_back_to_rest_quote(self) -> None:
        engine = _open_position(entry=100.0, stop=95.0, target=110.0)
        with patch("backend.api.websocket.get_prices_by_symbol", return_value={}), \
             patch.object(engine.client, "get_multiple_quotes",
                          return_value={"HDFCBANK": {"ltp": 94.0}}), \
             patch.object(engine, "_close_position") as mock_close:
            asyncio.run(engine._monitor_open_positions())
        mock_close.assert_awaited_once_with("HDFCBANK", "STOP_LOSS_HIT")

    def test_no_price_available_anywhere_does_not_guess(self) -> None:
        engine = _open_position()
        with patch("backend.api.websocket.get_prices_by_symbol", return_value={}), \
             patch.object(engine.client, "get_multiple_quotes", return_value={}), \
             patch.object(engine, "_close_position") as mock_close:
            asyncio.run(engine._monitor_open_positions())
        mock_close.assert_not_awaited()

    def test_trailing_stop_ratchets_before_exit_check(self) -> None:
        engine = _open_position(entry=100.0, stop=95.0, target=200.0)  # high target, won't hit
        with patch("backend.api.websocket.get_prices_by_symbol",
                   return_value={"HDFCBANK": {"ltp": 110.0}}), \
             patch.object(engine.client, "get_historical_candles", return_value=[]), \
             patch.object(engine, "_close_position") as mock_close:
            asyncio.run(engine._monitor_open_positions())
        # 2R move (100->110, risk=5) should ratchet the trailing stop to 105,
        # not close the position (price 110 > stop 105).
        assert engine._open_positions["HDFCBANK"]["trailing_stop"] == 105.0
        mock_close.assert_not_awaited()


class TestExecuteMultiSignal:
    def test_non_buy_signal_is_ignored(self) -> None:
        engine = _isolated_engine()
        sig = StrategySignal(strategy_name="EMA_TREND", symbol="TCS", signal=SignalType.NONE)
        assert engine.execute_multi_signal(sig) is None

    def test_buy_signal_opens_a_position(self) -> None:
        engine = _isolated_engine()
        sig = StrategySignal(strategy_name="EMA_TREND", symbol="TCS", signal=SignalType.BUY,
                              confidence=80.0, entry_price=100.0, stop_loss=95.0, target=110.0)
        sig.indicators = {"atr": 1.0, "rsi": 60.0, "volume_ratio": 1.5}
        trade_id = engine.execute_multi_signal(sig)
        assert trade_id is not None
        assert "TCS" in engine._open_positions
        assert engine._open_positions["TCS"]["strategy_name"] == "EMA_TREND"
        assert engine._open_positions["TCS"]["target"] == 110.0

    def test_risk_manager_can_block_a_trade(self) -> None:
        engine = _isolated_engine()
        with patch.object(engine.risk_manager, "can_take_trade", return_value=(False, "Max trades reached")):
            sig = StrategySignal(strategy_name="ORB", symbol="TCS", signal=SignalType.BUY,
                                  confidence=90.0, entry_price=100.0, stop_loss=95.0, target=110.0)
            assert engine.execute_multi_signal(sig) is None
            assert "TCS" not in engine._open_positions


class TestResolveWatchlist:
    def test_falls_back_to_default_on_error(self) -> None:
        engine = _isolated_engine()
        with patch("backend.config.universe_config.load_universe_config", side_effect=RuntimeError("boom")):
            watchlist = engine._resolve_watchlist()
        assert len(watchlist) > 0  # never returns an empty scan list on error


class TestDailyFloorTrade:
    def _fresh_signal(self, symbol, confidence, entry_price=100.0):
        sig = StrategySignal(strategy_name="EMA_TREND", symbol=symbol, signal=SignalType.NONE,
                              confidence=confidence, entry_price=entry_price, stop_loss=95.0, target=110.0)
        return [sig]

    def test_does_nothing_before_trigger_time(self) -> None:
        engine = _isolated_engine()
        engine._best_of_day = {"symbol": "TCS", "confidence": 65.0}
        from datetime import datetime
        early = datetime(2026, 1, 1, 10, 0)  # before default trigger (12:00)
        assert engine._maybe_take_daily_floor_trade(early) is None

    def test_does_nothing_if_a_real_trade_already_happened(self) -> None:
        engine = _isolated_engine()
        engine._best_of_day = {"symbol": "TCS", "confidence": 65.0}
        engine._trades_taken_today = 1
        from datetime import datetime
        at_trigger = datetime(2026, 1, 1, 12, 5)
        assert engine._maybe_take_daily_floor_trade(at_trigger) is None

    def test_does_nothing_without_a_tracked_candidate(self) -> None:
        engine = _isolated_engine()
        from datetime import datetime
        at_trigger = datetime(2026, 1, 1, 12, 5)
        assert engine._maybe_take_daily_floor_trade(at_trigger) is None

    def test_only_fires_once_per_day(self) -> None:
        engine = _isolated_engine()
        engine._best_of_day = {"symbol": "TCS", "confidence": 65.0}
        from datetime import datetime
        at_trigger = datetime(2026, 1, 1, 12, 5)
        with patch.object(engine, "evaluate_all_strategies", return_value=self._fresh_signal("TCS", 65.0)), \
             patch.object(engine, "execute_multi_signal", return_value="trade-1") as mock_exec:
            engine._maybe_take_daily_floor_trade(at_trigger)
            assert engine._daily_floor_taken is True
            engine._maybe_take_daily_floor_trade(at_trigger)  # second call same day
        mock_exec.assert_called_once()  # not called again

    def test_takes_the_trade_when_fresh_check_still_clears_floor(self) -> None:
        engine = _isolated_engine()
        engine._best_of_day = {"symbol": "TCS", "confidence": 65.0}
        from datetime import datetime
        at_trigger = datetime(2026, 1, 1, 12, 5)
        with patch.object(engine, "evaluate_all_strategies", return_value=self._fresh_signal("TCS", 70.0)), \
             patch.object(engine, "execute_multi_signal", return_value="trade-1") as mock_exec:
            result = engine._maybe_take_daily_floor_trade(at_trigger)
        assert result == "trade-1"
        mock_exec.assert_called_once()
        # the signal passed to execution must have been relabeled BUY
        passed_signal = mock_exec.call_args[0][0]
        assert passed_signal.signal == "BUY"

    def test_skips_when_fresh_check_no_longer_clears_floor(self) -> None:
        """The setup looked good hours ago but faded — must NOT force a
        trade just because it was the day's best-so-far."""
        engine = _isolated_engine()
        engine._best_of_day = {"symbol": "TCS", "confidence": 65.0}
        from datetime import datetime
        at_trigger = datetime(2026, 1, 1, 12, 5)
        with patch.object(engine, "evaluate_all_strategies", return_value=self._fresh_signal("TCS", 40.0)), \
             patch.object(engine, "execute_multi_signal") as mock_exec:
            result = engine._maybe_take_daily_floor_trade(at_trigger)
        assert result is None
        mock_exec.assert_not_called()

    def test_never_fires_when_disabled(self) -> None:
        engine = _isolated_engine()
        engine.enable_daily_floor_trade = False
        engine._best_of_day = {"symbol": "TCS", "confidence": 90.0}
        from datetime import datetime
        at_trigger = datetime(2026, 1, 1, 12, 5)
        with patch.object(engine, "execute_multi_signal") as mock_exec:
            engine._maybe_take_daily_floor_trade(at_trigger)
        mock_exec.assert_not_called()
