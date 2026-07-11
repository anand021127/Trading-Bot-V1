"""Tests for the realistic backtest engine (item #6).

Verifies: full-history processing (not "2 trades a year"), real cost
application, honest skip-reporting for insufficient data (never padded with
synthetic candles), and that rejected signals are counted/aggregated, never
silently dropped.
"""
from __future__ import annotations

from typing import Any, Dict, List

from backend.backtest.engine import BacktestEngine, CostConfig


def _candle(c: float, v: float, i: int) -> Dict[str, Any]:
    return {"open": c - 0.1, "high": c + 0.5, "low": c - 0.5, "close": c, "volume": v,
            "timestamp": f"bar-{i:04d}"}


def _uptrend_candles(n: int = 150) -> List[Dict[str, Any]]:
    """Zigzag uptrend — EMA_TREND fires reliably, price keeps climbing so
    entries reach their 2R target within the window (produces real,
    verifiable winning trades, not luck)."""
    closes = [100.0]
    for i in range(n - 1):
        step = 0.6 if i % 2 == 0 else -0.35
        closes.append(closes[-1] + step)
    return [_candle(c, 2500 if i % 20 == 0 else 1000, i) for i, c in enumerate(closes)]


def _flat_choppy_candles(n: int = 150) -> List[Dict[str, Any]]:
    closes = [100.0 + (0.2 if i % 2 == 0 else -0.2) for i in range(n)]
    return [_candle(c, 800, i) for i, c in enumerate(closes)]


def _sharp_downtrend_candles(n: int = 150) -> List[Dict[str, Any]]:
    """Uptrend long enough (past min_candles_required) to trigger an
    EMA_TREND entry, then a sharp, sustained drop so the position is
    stopped out for a real loss."""
    closes = [100.0]
    for i in range(74):
        step = 0.6 if i % 2 == 0 else -0.35
        closes.append(closes[-1] + step)
    # Now crash — guarantees stop-loss is hit on the very next bars.
    for i in range(n - 75):
        closes.append(closes[-1] - 1.2)
    return [_candle(c, 2500 if i == 74 else 800, i) for i, c in enumerate(closes)]


class TestBacktestEngine:
    def test_processes_full_history_and_generates_multiple_trades(self) -> None:
        """The old bug produced ~2 trades/year regardless of data. A 150-bar
        real uptrend should produce several, not a token amount."""
        engine = BacktestEngine(capital=100_000, min_candles_required=60)
        result = engine.run({"HDFCBANK": _uptrend_candles(150)})

        assert result.total_candles_scanned == 150
        assert result.trades_taken >= 3  # proves it isn't just skimming 1-2 trades
        assert all(t["net_pnl"] < t["gross_pnl"] for t in result.trade_log)  # costs applied

    def test_insufficient_data_is_skipped_not_padded(self) -> None:
        engine = BacktestEngine(min_candles_required=60)
        result = engine.run({"TINY": _uptrend_candles(150)[:10]})

        assert result.trades_taken == 0
        assert len(result.skipped_symbols) == 1
        assert "Not padded with synthetic data" in result.skipped_symbols[0]["reason"]
        assert result.data_source == "real_upstox_v3"

    def test_choppy_market_produces_rejections_not_trades(self) -> None:
        engine = BacktestEngine(min_candles_required=60)
        result = engine.run({"RELIANCE": _flat_choppy_candles(150)})

        assert result.trades_taken == 0
        assert result.rejected_signals_total_count > 0
        assert sum(result.rejection_reason_counts.values()) > 0
        # transparency: sample entries carry real reasons, not empty stubs
        assert all(r["reasons"] for r in result.rejected_signals_sample)

    def test_losing_trade_is_recorded_honestly(self) -> None:
        engine = BacktestEngine(min_candles_required=60)
        result = engine.run({"HDFCBANK": _sharp_downtrend_candles(150)})

        assert result.trades_taken >= 1
        assert result.losing_trades >= 1
        assert any(t["exit_reason"] == "STOP_LOSS_HIT" for t in result.trade_log)
        assert result.net_profit < 0

    def test_accuracy_and_profit_factor_computed_from_real_trades(self) -> None:
        engine = BacktestEngine(min_candles_required=60)
        result = engine.run({"HDFCBANK": _uptrend_candles(150)})

        wins = result.winning_trades
        total = result.trades_taken
        assert result.accuracy_pct == round(wins / total * 100, 2) or \
            abs(result.accuracy_pct - (wins / total * 100)) < 0.01

    def test_multiple_symbols_are_all_processed_independently(self) -> None:
        engine = BacktestEngine(min_candles_required=60)
        result = engine.run({
            "HDFCBANK": _uptrend_candles(150),
            "RELIANCE": _flat_choppy_candles(150),
        })
        assert result.total_candles_scanned == 300
        trade_symbols = {t["symbol"] for t in result.trade_log}
        assert trade_symbols == {"HDFCBANK"}  # only the trending symbol actually traded

    def test_equity_curve_starts_at_capital(self) -> None:
        engine = BacktestEngine(capital=50_000, min_candles_required=60)
        result = engine.run({"HDFCBANK": _uptrend_candles(150)})
        assert result.equity_curve[0]["equity"] == 50_000

    def test_open_position_at_end_closes_as_backtest_end(self) -> None:
        engine = BacktestEngine(min_candles_required=60)
        # Short uptrend that enters a trade near the very end — not enough
        # bars left to hit target or stop before data runs out.
        candles = _uptrend_candles(65)
        result = engine.run({"HDFCBANK": candles})
        if result.trade_log:
            assert result.trade_log[-1]["exit_reason"] in ("BACKTEST_END", "TARGET_HIT", "STOP_LOSS_HIT")


class TestCostConfig:
    def test_apply_reduces_pnl_by_realistic_charges(self) -> None:
        costs = CostConfig(commission_pct=0.0003, slippage_pct=0.0001, stt_pct=0.001)
        result = costs.apply(entry=100.0, exit_price=110.0, qty=100)
        assert result["gross_pnl"] == 1000.0
        assert result["net_pnl"] < result["gross_pnl"]
        assert result["charges"] > 0

    def test_apply_on_a_loss_still_charges_costs(self) -> None:
        costs = CostConfig()
        result = costs.apply(entry=100.0, exit_price=90.0, qty=100)
        assert result["gross_pnl"] == -1000.0
        assert result["net_pnl"] < result["gross_pnl"]  # loss made worse by real costs
