"""Tests for TradingEngine.get_open_positions_detail() — item #7.

Verifies every field the paper-trading dashboard needs (symbol, entry,
target, stop-loss, live trailing SL, current P&L, strategy used), and that
missing live price data results in null P&L rather than a fabricated
number.
"""
from __future__ import annotations

from unittest.mock import patch

from backend.strategy.trading_engine import TradingEngine


def _engine_with_position(symbol: str = "HDFCBANK") -> TradingEngine:
    engine = TradingEngine()
    engine._open_positions[symbol] = {
        "trade_id": "t1",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "target": 110.0,
        "trailing_stop": 95.0,
        "strategy_name": "EMA_TREND",
        "quantity": 50,
        "atr": 1.2,
        "side": "long",
        "entry_time": "2026-01-01T09:30:00+00:00",
    }
    return engine


class TestOpenPositionsDetail:
    def test_all_required_fields_present(self) -> None:
        engine = _engine_with_position()
        with patch("backend.api.websocket.get_prices_by_symbol", return_value={}):
            details = engine.get_open_positions_detail()

        assert len(details) == 1
        d = details[0]
        for field in ("symbol", "strategy_used", "entry_price", "target",
                      "stop_loss", "trailing_stop", "current_price",
                      "current_pnl", "quantity"):
            assert field in d

    def test_no_live_tick_gives_null_pnl_not_fabricated(self) -> None:
        engine = _engine_with_position()
        with patch("backend.api.websocket.get_prices_by_symbol", return_value={}):
            details = engine.get_open_positions_detail()

        assert details[0]["current_price"] is None
        assert details[0]["current_pnl"] is None

    def test_live_tick_computes_real_pnl(self) -> None:
        engine = _engine_with_position()
        with patch("backend.api.websocket.get_prices_by_symbol",
                    return_value={"HDFCBANK": {"ltp": 106.0}}):
            details = engine.get_open_positions_detail()

        d = details[0]
        assert d["current_price"] == 106.0
        assert d["current_pnl"] == (106.0 - 100.0) * 50
        assert d["current_pnl_pct"] == 6.0

    def test_trailing_stop_ratchets_up_as_price_rises(self) -> None:
        engine = _engine_with_position()  # risk = 5.0 (100 - 95)
        with patch("backend.api.websocket.get_prices_by_symbol",
                    return_value={"HDFCBANK": {"ltp": 110.0}}):  # 2R move
            details = engine.get_open_positions_detail()

        assert details[0]["trailing_stop"] == 105.0  # stage 3: lock 1.0R
        assert details[0]["trailing_stop"] > 95.0

    def test_strategy_used_reflects_what_opened_the_position(self) -> None:
        engine = _engine_with_position()
        with patch("backend.api.websocket.get_prices_by_symbol", return_value={}):
            details = engine.get_open_positions_detail()
        assert details[0]["strategy_used"] == "EMA_TREND"

    def test_no_open_positions_returns_empty_list(self) -> None:
        engine = TradingEngine()
        assert engine.get_open_positions_detail() == []
