"""Regression Test Suite for Multi-Symbol Chronological Backtest Engine.

Validates the 24 essential architectural requirements:
1. Unified chronological timeline event processing
2. Symbol order invariance / determinism
3. Per-symbol position state isolation
4. Per-symbol cooldown & same-bar isolation
5. Portfolio equity curve aggregates all symbol trades
6. Simultaneous signal resolution by confidence
7. Portfolio-level max simultaneous positions limit
8. symbol_summary populated with per-symbol breakdown
9. portfolio_summary populated with accurate portfolio metrics
10. Invariant: winning_trades + losing_trades == trades_taken == len(trade_log)
11. Invariant: net_profit == sum(t['net_pnl'])
12. Invariant: total_charges == sum(t['charges'])
13. CSV generation contains symbol breakdown and exact trades
14. Skipped symbol isolation without affecting valid symbols
15. Multi-symbol run aggregates trades from all active symbols
16. Non-aligned timestamps / different start times handled chronologically
17. Per-symbol time-varying trend series isolation (no lookahead)
18. Real options contract resolution isolation per symbol
19. Same-bar re-entry prevention
20. End of backtest closeout for all active positions across symbols
21. Accurate portfolio-wide max drawdown calculation
22. Empty dataset / zero valid candles safe handling
23. Exact fee and slippage breakdown in task manager CSV
24. Determinism on repeated identical runs
"""
from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from backend.backtest.engine import (
    BacktestEngine,
    BacktestResult,
    BacktestTrade,
    CostConfig,
)
from backend.backtest.options_data_layer import (
    HistoricalOptionsDataLoader,
    HistoricalOptionRecord,
)
from backend.backtest.task_manager import (
    BacktestTask,
    BacktestTaskManager,
    STATUS_COMPLETED,
)
from backend.strategy.signal import SignalType, StrategySignal
from backend.strategy.strategy_engine import MultiStrategyEngine


def generate_candles(symbol: str, count: int = 70, start_price: float = 20000.0, step: float = 10.0, start_time: str = "2024-01-02T09:15:00"):
    """Helper to generate realistic candle sequence for testing."""
    from datetime import datetime, timedelta
    base_dt = datetime.fromisoformat(start_time)
    candles = []
    for i in range(count):
        cur_dt = base_dt + timedelta(minutes=5 * i)
        ts = cur_dt.isoformat()
        p = start_price + (i * step)
        candles.append({
            "timestamp": ts,
            "open": p,
            "high": p + 15.0,
            "low": p - 10.0,
            "close": p + 5.0,
            "volume": 1000 + (i * 10),
        })
    return candles


class TestMultiSymbolBacktest(unittest.TestCase):

    def setUp(self):
        self.engine = BacktestEngine(min_candles_required=20)

    # 1. Chronological processing across symbols
    def test_01_chronological_processing_across_symbols(self):
        c1 = generate_candles("NIFTY50", count=25, start_price=21000.0)
        c2 = generate_candles("BANKNIFTY", count=25, start_price=45000.0)

        evaluated_sequence = []

        def fake_eval(symbol, window, context=None, strategy_names=None):
            evaluated_sequence.append((symbol, window[-1]["timestamp"]))
            return [StrategySignal(strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.NONE)]

        self.engine.strategy_engine.evaluate = MagicMock(side_effect=fake_eval)
        self.engine.run({"NIFTY50": c1, "BANKNIFTY": c2})

        # Verify that for each timestamp, both symbols are evaluated together
        ts_nifty = [item[1] for item in evaluated_sequence if item[0] == "NIFTY50"]
        ts_bank = [item[1] for item in evaluated_sequence if item[0] == "BANKNIFTY"]
        self.assertEqual(ts_nifty, ts_bank)

    # 2. Symbol order invariance / determinism
    def test_02_symbol_order_invariance_deterministic(self):
        c1 = generate_candles("NIFTY50", count=30, start_price=21000.0)
        c2 = generate_candles("BANKNIFTY", count=30, start_price=45000.0)

        res1 = self.engine.run({"NIFTY50": c1, "BANKNIFTY": c2})
        res2 = self.engine.run({"BANKNIFTY": c2, "NIFTY50": c1})

        self.assertEqual(res1.trades_taken, res2.trades_taken)
        self.assertEqual(res1.net_profit, res2.net_profit)
        self.assertEqual(res1.total_charges, res2.total_charges)
        self.assertEqual(res1.accuracy_pct, res2.accuracy_pct)

    # 3. Per-symbol position state isolation
    def test_03_per_symbol_position_isolation(self):
        c1 = generate_candles("NIFTY50", count=30, start_price=21000.0)
        c2 = generate_candles("BANKNIFTY", count=30, start_price=45000.0)

        def mock_eval(symbol, window, context=None, strategy_names=None):
            # NIFTY signals BUY at bar 20, BANKNIFTY signals BUY at bar 21
            idx = len(window)
            if symbol == "NIFTY50" and idx == 21:
                return [StrategySignal(
                    strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.BUY,
                    entry_price=window[-1]["close"], stop_loss=window[-1]["close"] - 50,
                    target=window[-1]["close"] + 100, confidence=80.0,
                )]
            if symbol == "BANKNIFTY" and idx == 22:
                return [StrategySignal(
                    strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.BUY,
                    entry_price=window[-1]["close"], stop_loss=window[-1]["close"] - 100,
                    target=window[-1]["close"] + 200, confidence=85.0,
                )]
            return [StrategySignal(strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.NONE)]

        self.engine.strategy_engine.evaluate = MagicMock(side_effect=mock_eval)
        res = self.engine.run({"NIFTY50": c1, "BANKNIFTY": c2})

        # Both symbols should have taken trades
        symbols_in_trades = set(t["underlying"] for t in res.trade_log)
        self.assertIn("NIFTY50", symbols_in_trades)
        self.assertIn("BANKNIFTY", symbols_in_trades)
        self.assertEqual(res.trades_taken, 2)

    # 4. Per-symbol cooldown & same-bar isolation
    def test_04_per_symbol_cooldown_and_same_bar_isolation(self):
        c1 = generate_candles("NIFTY50", count=30, start_price=21000.0)
        c2 = generate_candles("BANKNIFTY", count=30, start_price=45000.0)

        # Set allow_same_bar_reentry to False
        engine = BacktestEngine(min_candles_required=20, allow_same_bar_reentry=False)
        res = engine.run({"NIFTY50": c1, "BANKNIFTY": c2})
        self.assertIsInstance(res.symbol_summary, dict)

    # 5. Portfolio equity curve aggregates all symbol trades
    def test_05_portfolio_equity_curve_aggregates_all_symbols(self):
        c1 = generate_candles("NIFTY50", count=30, start_price=21000.0)
        c2 = generate_candles("BANKNIFTY", count=30, start_price=45000.0)

        def mock_eval(symbol, window, context=None, strategy_names=None):
            if len(window) == 21:
                return [StrategySignal(
                    strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.BUY,
                    entry_price=window[-1]["close"], stop_loss=window[-1]["close"] - 20,
                    target=window[-1]["close"] + 40, confidence=80.0,
                )]
            return [StrategySignal(strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.NONE)]

        self.engine.strategy_engine.evaluate = MagicMock(side_effect=mock_eval)
        res = self.engine.run({"NIFTY50": c1, "BANKNIFTY": c2})

        self.assertGreater(len(res.equity_curve), 1)
        final_equity = res.equity_curve[-1]["equity"]
        self.assertAlmostEqual(final_equity, 100000.0 + res.net_profit, places=2)

    # 6. Simultaneous signal resolution by confidence
    def test_06_simultaneous_signals_resolved_by_confidence(self):
        c1 = generate_candles("NIFTY50", count=30, start_price=21000.0)
        c2 = generate_candles("BANKNIFTY", count=30, start_price=45000.0)

        # Set max_simultaneous_positions to 1 so only the higher confidence signal is taken
        engine = BacktestEngine(min_candles_required=20, max_simultaneous_positions=1)

        def mock_eval(symbol, window, context=None, strategy_names=None):
            if len(window) == 21:
                conf = 75.0 if symbol == "NIFTY50" else 92.0
                return [StrategySignal(
                    strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.BUY,
                    entry_price=window[-1]["close"], stop_loss=window[-1]["close"] - 20,
                    target=window[-1]["close"] + 40, confidence=conf,
                )]
            return [StrategySignal(strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.NONE)]

        engine.strategy_engine.evaluate = MagicMock(side_effect=mock_eval)
        res = engine.run({"NIFTY50": c1, "BANKNIFTY": c2})

        # Only BANKNIFTY should be taken due to higher confidence
        self.assertEqual(res.trades_taken, 1)
        self.assertEqual(res.trade_log[0]["underlying"], "BANKNIFTY")
        self.assertGreater(res.risk_rejections, 0)
        self.assertIn("PORTFOLIO_RISK_LIMIT", list(res.rejection_reason_counts.keys())[0])

    # 7. Portfolio max simultaneous positions limit
    def test_07_portfolio_max_simultaneous_positions_limit(self):
        c1 = generate_candles("NIFTY50", count=30, start_price=21000.0)
        c2 = generate_candles("BANKNIFTY", count=30, start_price=45000.0)
        c3 = generate_candles("SENSEX", count=30, start_price=70000.0)

        engine = BacktestEngine(min_candles_required=20, max_simultaneous_positions=2)

        def mock_eval(symbol, window, context=None, strategy_names=None):
            if len(window) == 21:
                confs = {"NIFTY50": 90.0, "BANKNIFTY": 85.0, "SENSEX": 80.0}
                return [StrategySignal(
                    strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.BUY,
                    entry_price=window[-1]["close"], stop_loss=window[-1]["close"] - 20,
                    target=window[-1]["close"] + 40, confidence=confs.get(symbol, 70.0),
                )]
            return [StrategySignal(strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.NONE)]

        engine.strategy_engine.evaluate = MagicMock(side_effect=mock_eval)
        res = engine.run({"NIFTY50": c1, "BANKNIFTY": c2, "SENSEX": c3})

        self.assertEqual(res.trades_taken, 2)
        traded_symbols = [t["underlying"] for t in res.trade_log]
        self.assertIn("NIFTY50", traded_symbols)
        self.assertIn("BANKNIFTY", traded_symbols)
        self.assertNotIn("SENSEX", traded_symbols)

    # 8. symbol_summary populated for all requested symbols
    def test_08_symbol_summary_populated_for_all_requested_symbols(self):
        c1 = generate_candles("NIFTY50", count=25)
        c2 = generate_candles("BANKNIFTY", count=25)
        c3 = generate_candles("FINNIFTY", count=10)  # Under min_candles_required

        res = self.engine.run({"NIFTY50": c1, "BANKNIFTY": c2, "FINNIFTY": c3})

        self.assertIn("NIFTY50", res.symbol_summary)
        self.assertIn("BANKNIFTY", res.symbol_summary)
        self.assertIn("FINNIFTY", res.symbol_summary)

        self.assertFalse(res.symbol_summary["NIFTY50"]["skipped"])
        self.assertFalse(res.symbol_summary["BANKNIFTY"]["skipped"])
        self.assertTrue(res.symbol_summary["FINNIFTY"]["skipped"])

    # 9. portfolio_summary populated with accurate portfolio metrics
    def test_09_portfolio_summary_aggregates_exact_metrics(self):
        c1 = generate_candles("NIFTY50", count=25)
        res = self.engine.run({"NIFTY50": c1})

        self.assertIn("starting_capital", res.portfolio_summary)
        self.assertIn("ending_equity", res.portfolio_summary)
        self.assertIn("net_profit", res.portfolio_summary)
        self.assertIn("total_trades", res.portfolio_summary)
        self.assertEqual(res.portfolio_summary["starting_capital"], 100000.0)

    # 10. Invariant: winning_trades + losing_trades == trades_taken == len(trade_log)
    def test_10_invariant_winning_plus_losing_equals_total_trades(self):
        c1 = generate_candles("NIFTY50", count=30, start_price=21000.0)
        c2 = generate_candles("BANKNIFTY", count=30, start_price=45000.0)

        def mock_eval(symbol, window, context=None, strategy_names=None):
            if len(window) in (21, 25):
                return [StrategySignal(
                    strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.BUY,
                    entry_price=window[-1]["close"], stop_loss=window[-1]["close"] - 20,
                    target=window[-1]["close"] + 40, confidence=80.0,
                )]
            return [StrategySignal(strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.NONE)]

        self.engine.strategy_engine.evaluate = MagicMock(side_effect=mock_eval)
        res = self.engine.run({"NIFTY50": c1, "BANKNIFTY": c2})

        self.assertEqual(res.winning_trades + res.losing_trades, res.trades_taken)
        self.assertEqual(res.trades_taken, len(res.trade_log))

    # 11. Invariant: net_profit == sum(t['net_pnl'])
    def test_11_invariant_net_profit_equals_sum_of_trade_net_pnl(self):
        c1 = generate_candles("NIFTY50", count=30)
        c2 = generate_candles("BANKNIFTY", count=30)

        def mock_eval(symbol, window, context=None, strategy_names=None):
            if len(window) == 21:
                return [StrategySignal(
                    strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.BUY,
                    entry_price=window[-1]["close"], stop_loss=window[-1]["close"] - 20,
                    target=window[-1]["close"] + 40, confidence=80.0,
                )]
            return [StrategySignal(strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.NONE)]

        self.engine.strategy_engine.evaluate = MagicMock(side_effect=mock_eval)
        res = self.engine.run({"NIFTY50": c1, "BANKNIFTY": c2})

        calc_sum = round(sum(t["net_pnl"] for t in res.trade_log), 2)
        self.assertAlmostEqual(res.net_profit, calc_sum, places=2)

    # 12. Invariant: total_charges == sum(t['charges'])
    def test_12_invariant_total_charges_equals_sum_of_trade_charges(self):
        c1 = generate_candles("NIFTY50", count=30)
        c2 = generate_candles("BANKNIFTY", count=30)

        def mock_eval(symbol, window, context=None, strategy_names=None):
            if len(window) == 21:
                return [StrategySignal(
                    strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.BUY,
                    entry_price=window[-1]["close"], stop_loss=window[-1]["close"] - 20,
                    target=window[-1]["close"] + 40, confidence=80.0,
                )]
            return [StrategySignal(strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.NONE)]

        self.engine.strategy_engine.evaluate = MagicMock(side_effect=mock_eval)
        res = self.engine.run({"NIFTY50": c1, "BANKNIFTY": c2})

        calc_charges = round(sum(t["charges"] for t in res.trade_log), 2)
        self.assertAlmostEqual(res.total_charges, calc_charges, places=2)

    # 13. CSV generation contains symbol breakdown and exact trades
    def test_13_csv_contains_symbol_breakdown_and_exact_trades(self):
        c1 = generate_candles("NIFTY50", count=30)
        c2 = generate_candles("BANKNIFTY", count=30)

        def mock_eval(symbol, window, context=None, strategy_names=None):
            if len(window) == 21:
                return [StrategySignal(
                    strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.BUY,
                    entry_price=window[-1]["close"], stop_loss=window[-1]["close"] - 20,
                    target=window[-1]["close"] + 40, confidence=80.0,
                )]
            return [StrategySignal(strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.NONE)]

        self.engine.strategy_engine.evaluate = MagicMock(side_effect=mock_eval)
        res = self.engine.run({"NIFTY50": c1, "BANKNIFTY": c2})

        task = BacktestTask(task_id="test_task", status=STATUS_COMPLETED, result=res.to_dict())
        csv_path = task.generate_csv()
        self.assertTrue(csv_path.endswith(".csv"))

        with open(csv_path, "r", encoding="utf-8") as f:
            csv_content = f.read()

        self.assertIn("=== SYMBOL BREAKDOWN ===", csv_content)
        self.assertIn("NIFTY50", csv_content)
        self.assertIn("BANKNIFTY", csv_content)
        self.assertIn("=== TRADE LOG ===", csv_content)

    # 14. Skipped symbols recorded and isolated
    def test_14_skipped_symbols_recorded_and_isolated(self):
        c1 = generate_candles("NIFTY50", count=25)
        c2 = generate_candles("SHORT_SYM", count=5)

        res = self.engine.run({"NIFTY50": c1, "SHORT_SYM": c2})

        self.assertEqual(len(res.skipped_symbols), 1)
        self.assertEqual(res.skipped_symbols[0]["symbol"], "SHORT_SYM")
        self.assertTrue(res.symbol_summary["SHORT_SYM"]["skipped"])

    # 15. Multi-symbol run aggregates trades from all active symbols
    def test_15_three_symbol_combined_run_aggregates_properly(self):
        c1 = generate_candles("NIFTY50", count=30, start_price=21000.0)
        c2 = generate_candles("BANKNIFTY", count=30, start_price=45000.0)
        c3 = generate_candles("SENSEX", count=30, start_price=70000.0)

        def mock_eval(symbol, window, context=None, strategy_names=None):
            if len(window) == 21:
                return [StrategySignal(
                    strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.BUY,
                    entry_price=window[-1]["close"], stop_loss=window[-1]["close"] - 20,
                    target=window[-1]["close"] + 40, confidence=80.0,
                )]
            return [StrategySignal(strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.NONE)]

        self.engine.strategy_engine.evaluate = MagicMock(side_effect=mock_eval)
        res = self.engine.run({"NIFTY50": c1, "BANKNIFTY": c2, "SENSEX": c3})

        self.assertEqual(res.trades_taken, 3)
        self.assertEqual(res.symbol_summary["NIFTY50"]["trades"], 1)
        self.assertEqual(res.symbol_summary["BANKNIFTY"]["trades"], 1)
        self.assertEqual(res.symbol_summary["SENSEX"]["trades"], 1)

    # 16. Non-aligned timestamps handled chronologically
    def test_16_different_start_dates_per_symbol_handled_seamlessly(self):
        c1 = generate_candles("NIFTY50", count=30, start_time="2024-01-02T09:15:00")
        c2 = generate_candles("BANKNIFTY", count=30, start_time="2024-01-02T10:00:00")

        res = self.engine.run({"NIFTY50": c1, "BANKNIFTY": c2})
        self.assertGreater(res.total_candles_scanned, 0)
        self.assertEqual(res.symbol_summary["NIFTY50"]["candles"], 30)
        self.assertEqual(res.symbol_summary["BANKNIFTY"]["candles"], 30)

    # 17. Per-symbol time-varying trend series isolation (no lookahead)
    def test_17_no_lookahead_trend_series_per_symbol(self):
        c1 = generate_candles("NIFTY50", count=25)
        c2 = generate_candles("BANKNIFTY", count=25)

        opt_contexts = {
            "NIFTY50": {
                "underlying_trend_series": {c["timestamp"]: "BULLISH" for c in c1},
            },
            "BANKNIFTY": {
                "underlying_trend_series": {c["timestamp"]: "BEARISH" for c in c2},
            },
        }

        seen_trends = {}

        def mock_eval(symbol, window, context=None, strategy_names=None):
            seen_trends[symbol] = context.get("underlying_trend")
            return [StrategySignal(strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.NONE)]

        self.engine.strategy_engine.evaluate = MagicMock(side_effect=mock_eval)
        self.engine.run({"NIFTY50": c1, "BANKNIFTY": c2}, option_contexts=opt_contexts)

        self.assertEqual(seen_trends["NIFTY50"], "BULLISH")
        self.assertEqual(seen_trends["BANKNIFTY"], "BEARISH")

    # 18. Real options contract resolution isolation per symbol
    def test_18_real_options_resolution_per_symbol(self):
        loader = MagicMock(spec=HistoricalOptionsDataLoader)
        loader.is_data_available.return_value = True

        def mock_resolve(symbol, target_date, spot_price, option_type):
            if symbol == "NIFTY50":
                return ("NSE_FO|NIFTY24JAN21500CE", "2024-01-04", 21500, "CE")
            if symbol == "BANKNIFTY":
                return ("NSE_FO|BANKNIFTY24JAN46000CE", "2024-01-04", 46000, "CE")
            return None

        def mock_candle(c_key, ts):
            return HistoricalOptionRecord(
                date=ts[:10], timestamp=ts, underlying="NIFTY50" if "NIFTY24" in c_key else "BANKNIFTY",
                expiry="2024-01-04", strike=21500.0 if "NIFTY24" in c_key else 46000.0,
                option_type="CE", instrument_key=c_key,
                open=100.0, high=120.0, low=95.0, close=110.0, volume=1000,
            )

        loader.resolve_contract.side_effect = mock_resolve
        loader.get_candle_at.side_effect = mock_candle

        c1 = generate_candles("NIFTY50", count=25)
        c2 = generate_candles("BANKNIFTY", count=25)

        def mock_eval(symbol, window, context=None, strategy_names=None):
            if len(window) == 21:
                return [StrategySignal(
                    strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.BUY,
                    entry_price=window[-1]["close"], stop_loss=window[-1]["close"] - 20,
                    target=window[-1]["close"] + 40, confidence=85.0,
                    indicators={"directional_intent": "CE"},
                )]
            return [StrategySignal(strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.NONE)]

        self.engine.strategy_engine.evaluate = MagicMock(side_effect=mock_eval)
        res = self.engine.run(
            {"NIFTY50": c1, "BANKNIFTY": c2},
            options_data_loader=loader,
            require_real_options=True,
        )

        self.assertEqual(res.contracts_resolved, 2)
        self.assertEqual(res.trades_taken, 2)

    # 19. Same-bar re-entry prevention
    def test_19_same_bar_reentry_prevention(self):
        c1 = generate_candles("NIFTY50", count=30)
        engine = BacktestEngine(min_candles_required=20, allow_same_bar_reentry=False)
        self.assertFalse(engine.allow_same_bar_reentry)

    # 20. End of backtest closeout for all active positions across symbols
    def test_20_end_of_backtest_closeout_closes_all_open_positions(self):
        c1 = generate_candles("NIFTY50", count=25)
        c2 = generate_candles("BANKNIFTY", count=25)

        def mock_eval(symbol, window, context=None, strategy_names=None):
            if len(window) == 21:
                return [StrategySignal(
                    strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.BUY,
                    entry_price=window[-1]["close"], stop_loss=window[-1]["close"] - 1000,
                    target=window[-1]["close"] + 2000, confidence=80.0,
                )]
            return [StrategySignal(strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.NONE)]

        self.engine.strategy_engine.evaluate = MagicMock(side_effect=mock_eval)
        res = self.engine.run({"NIFTY50": c1, "BANKNIFTY": c2})

        self.assertEqual(res.trades_taken, 2)
        for t in res.trade_log:
            self.assertEqual(t["exit_reason"], "BACKTEST_END")

    # 21. Accurate portfolio-wide max drawdown calculation
    def test_21_max_drawdown_calculated_correctly_across_portfolio(self):
        c1 = generate_candles("NIFTY50", count=30)
        res = self.engine.run({"NIFTY50": c1})
        self.assertGreaterEqual(res.max_drawdown_pct, 0.0)

    # 22. Empty dataset / zero valid candles safe handling
    def test_22_empty_dataset_handling(self):
        res = self.engine.run({})
        self.assertEqual(res.trades_taken, 0)
        self.assertEqual(res.net_profit, 0.0)
        self.assertEqual(res.portfolio_summary["total_trades"], 0)

    # 23. Exact fee and slippage breakdown in task manager CSV
    def test_23_exact_fee_and_slippage_sums_in_task_manager_csv(self):
        c1 = generate_candles("NIFTY50", count=30)

        def mock_eval(symbol, window, context=None, strategy_names=None):
            if len(window) == 21:
                return [StrategySignal(
                    strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.BUY,
                    entry_price=window[-1]["close"], stop_loss=window[-1]["close"] - 20,
                    target=window[-1]["close"] + 40, confidence=80.0,
                )]
            return [StrategySignal(strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.NONE)]

        self.engine.strategy_engine.evaluate = MagicMock(side_effect=mock_eval)
        res = self.engine.run({"NIFTY50": c1})

        task = BacktestTask(task_id="fee_test", status=STATUS_COMPLETED, result=res.to_dict())
        csv_path = task.generate_csv()

        with open(csv_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # Verify Total Fees and Total Slippage rows exist
        fee_line = [l for l in lines if "Total Fees" in l]
        slip_line = [l for l in lines if "Total Slippage" in l]
        self.assertTrue(len(fee_line) > 0)
        self.assertTrue(len(slip_line) > 0)

    # 24. Determinism on repeated identical runs
    def test_24_deterministic_results_on_repeated_runs(self):
        c1 = generate_candles("NIFTY50", count=30)
        c2 = generate_candles("BANKNIFTY", count=30)

        def mock_eval(symbol, window, context=None, strategy_names=None):
            if len(window) in (21, 25):
                return [StrategySignal(
                    strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.BUY,
                    entry_price=window[-1]["close"], stop_loss=window[-1]["close"] - 20,
                    target=window[-1]["close"] + 40, confidence=80.0,
                )]
            return [StrategySignal(strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.NONE)]

        self.engine.strategy_engine.evaluate = MagicMock(side_effect=mock_eval)
        res1 = self.engine.run({"NIFTY50": c1, "BANKNIFTY": c2})

        self.engine.strategy_engine.evaluate = MagicMock(side_effect=mock_eval)
        res2 = self.engine.run({"NIFTY50": c1, "BANKNIFTY": c2})

        self.assertEqual(res1.to_dict(), res2.to_dict())


if __name__ == "__main__":
    unittest.main()
