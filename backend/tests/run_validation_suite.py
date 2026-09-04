"""Focused Validation Suite for Multi-Symbol Chronological Backtest Engine.

Validates:
TEST 1: NIFTY50 ONLY
TEST 2: BANKNIFTY ONLY
TEST 3: SENSEX ONLY
TEST 4: SIX SYMBOL COMBINED (NIFTY50, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX, BANKEX)
TEST 5: SYMBOL ORDER INVARIANCE (Run A vs Run B)
Plus all critical invariant checks, chronological ordering checks, and rejection classifications.
"""
import csv
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.backtest.engine import BacktestEngine, CostConfig
from backend.backtest.options_data_layer import HistoricalOptionsDataLoader
from backend.backtest.historical_data_io import load_dataset_safe
from backend.backtest.task_manager import BacktestTask, STATUS_COMPLETED
from backend.indicators.ema import calculate_ema
from backend.indicators.choppiness import choppiness_index


def build_trend_series(candles: List[Dict[str, Any]], ema_fast: int = 20, ema_slow: int = 50, ci_period: int = 14) -> Dict[str, str]:
    if len(candles) < ema_slow:
        return {}
    closes = [float(c["close"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    ema_fast_vals = calculate_ema(closes, ema_fast)
    ema_slow_vals = calculate_ema(closes, ema_slow)
    ci_vals = choppiness_index(highs, lows, closes, ci_period)
    ci_offset = len(closes) - len(ci_vals)
    series: Dict[str, str] = {}
    for i in range(ema_slow - 1, len(closes)):
        ts = candles[i].get("timestamp")
        if not ts:
            continue
        ci_idx = i - ci_offset
        if 0 <= ci_idx < len(ci_vals) and ci_vals[ci_idx] > 61.8:
            series[ts] = "NEUTRAL"
        elif ema_fast_vals[i] > ema_slow_vals[i] and closes[i] > ema_fast_vals[i]:
            series[ts] = "BULLISH"
        elif ema_fast_vals[i] < ema_slow_vals[i]:
            series[ts] = "BEARISH"
        else:
            series[ts] = "NEUTRAL"
    return series


_DATA_CACHE: Dict[str, Tuple[List[Dict[str, Any]], Dict[str, Any]]] = {}


def load_symbol_data(symbols: List[str]) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Dict[str, Any]]]:
    root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    symbol_candles = {}
    option_contexts = {}

    for sym in symbols:
        if sym not in _DATA_CACHE:
            file_path = os.path.join(root_dir, "real_data", f"{sym}_2024_5min.json")
            candles = load_dataset_safe(file_path, auto_repair=True)
            trend_series = build_trend_series(candles)
            _DATA_CACHE[sym] = (candles, {
                "underlying_trend_series": trend_series,
                "symbol": sym,
            })
        candles, ctx = _DATA_CACHE[sym]
        symbol_candles[sym] = candles
        option_contexts[sym] = ctx

    return symbol_candles, option_contexts


_OPTIONS_LOADER: Optional[HistoricalOptionsDataLoader] = None


def get_options_data_loader() -> HistoricalOptionsDataLoader:
    global _OPTIONS_LOADER
    if _OPTIONS_LOADER is None:
        _OPTIONS_LOADER = HistoricalOptionsDataLoader(auto_load_cache=True)
    return _OPTIONS_LOADER


def run_engine_on_symbols(symbols: List[str]):
    symbol_candles, option_contexts = load_symbol_data(symbols)
    engine = BacktestEngine(costs=CostConfig(), capital=100000.0, risk_pct_per_trade=0.01)

    last_print = [0]
    def on_progress(p):
        cur = p.get("bar_index", 0)
        tot = p.get("total_bars", 1)
        if cur - last_print[0] >= 10000 or cur == tot:
            last_print[0] = cur
            pct = (cur / tot * 100) if tot else 0
            print(f"  .. progress: {cur}/{tot} bars ({pct:.1f}%), trades={p.get('trades_so_far', 0)}", flush=True)

    result = engine.run(
        symbol_candles=symbol_candles,
        strategy_names=["OPTION_PREMIUM"],
        option_contexts=option_contexts,
        progress_callback=on_progress,
    )
    return result


import pickle
import os

def run_invariance(res_a=None):
    print("\n============================================================")
    print("SYMBOL-ORDER INVARIANCE TEST")
    print("============================================================")
    order_a = ["NIFTY50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"]
    order_b = ["BANKEX", "SENSEX", "MIDCPNIFTY", "FINNIFTY", "BANKNIFTY", "NIFTY50"]

    if res_a is None:
        if os.path.exists("/tmp/res_combined.pkl"):
            print("Loading Run A from cached /tmp/res_combined.pkl...")
            with open("/tmp/res_combined.pkl", "rb") as f:
                res_a = pickle.load(f)
        else:
            print("Evaluating Run A (Forward order)...")
            res_a = run_engine_on_symbols(order_a)

    print("Evaluating Run B (Reversed order)...")
    res_b = run_engine_on_symbols(order_b)

    print(f"Run A total trades: {res_a.trades_taken}, Run B total trades: {res_b.trades_taken}")
    print(f"Run A net P&L:      ₹{res_a.net_profit:.2f}, Run B net P&L:      ₹{res_b.net_profit:.2f}")
    print(f"Run A charges:      ₹{res_a.total_charges:.2f}, Run B charges:      ₹{res_b.total_charges:.2f}")
    print(f"Run A drawdown:     {res_a.max_drawdown_pct:.2f}%, Run B drawdown:     {res_b.max_drawdown_pct:.2f}%")

    assert res_a.trades_taken == res_b.trades_taken, "Order variance: trades_taken differ"
    assert abs(res_a.net_profit - res_b.net_profit) < 0.01, "Order variance: net_profit differs"
    assert abs(res_a.total_charges - res_b.total_charges) < 0.01, "Order variance: total_charges differ"
    assert abs(res_a.max_drawdown_pct - res_b.max_drawdown_pct) < 0.01, "Order variance: max_drawdown differs"

    for i in range(len(res_a.trade_log)):
        t_a = res_a.trade_log[i]
        t_b = res_b.trade_log[i]
        assert t_a["entry_time"] == t_b["entry_time"], f"Trade {i} entry_time mismatch: {t_a['entry_time']} vs {t_b['entry_time']}"
        assert t_a["symbol"] == t_b["symbol"], f"Trade {i} symbol mismatch: {t_a['symbol']} vs {t_b['symbol']}"
        assert t_a["entry_price"] == t_b["entry_price"], f"Trade {i} entry_price mismatch"
        assert t_a["exit_price"] == t_b["exit_price"], f"Trade {i} exit_price mismatch"
        assert t_a["net_pnl"] == t_b["net_pnl"], f"Trade {i} net_pnl mismatch"

    print("\nSUCCESS: Order invariance verified! Run A and Run B produce 100% IDENTICAL trade logs and metrics.")


def main():
    if len(sys.argv) > 1 and ("invariance" in sys.argv[1] or "-i" in sys.argv[1]):
        run_invariance()
        return

    print("=" * 80)
    print("STARTING FOCUSED VALIDATION SUITE")
    print("=" * 80)

    # -------------------------------------------------------------
    # TEST 1 — NIFTY ONLY
    # -------------------------------------------------------------
    res_nifty = run_engine_on_symbols(["NIFTY50"])
    gross_nifty = sum(t.get("gross_pnl", 0) for t in res_nifty.trade_log)
    slip_nifty = sum(t.get("slippage", 0) for t in res_nifty.trade_log)

    print("\n============================================================")
    print("TEST 1 — NIFTY ONLY")
    print("============================================================")
    print(f"candles:              {res_nifty.total_candles_scanned}")
    print(f"signals:              {res_nifty.signals_generated}")
    print(f"trades:               {res_nifty.trades_taken}")
    print(f"wins:                 {res_nifty.winning_trades}")
    print(f"losses:               {res_nifty.losing_trades}")
    print(f"win rate:             {res_nifty.accuracy_pct:.2f}%")
    print(f"gross P&L:            ₹{gross_nifty:.2f}")
    print(f"charges:              ₹{res_nifty.total_charges:.2f}")
    print(f"slippage:             ₹{slip_nifty:.2f}")
    print(f"net P&L:              ₹{res_nifty.net_profit:.2f}")
    print(f"max drawdown:         {res_nifty.max_drawdown_pct:.2f}%")

    # -------------------------------------------------------------
    # TEST 2 — BANKNIFTY ONLY
    # -------------------------------------------------------------
    res_banknifty = run_engine_on_symbols(["BANKNIFTY"])
    gross_banknifty = sum(t.get("gross_pnl", 0) for t in res_banknifty.trade_log)
    slip_banknifty = sum(t.get("slippage", 0) for t in res_banknifty.trade_log)

    print("\n============================================================")
    print("TEST 2 — BANKNIFTY ONLY")
    print("============================================================")
    print(f"candles:              {res_banknifty.total_candles_scanned}")
    print(f"signals:              {res_banknifty.signals_generated}")
    print(f"trades:               {res_banknifty.trades_taken}")
    print(f"wins:                 {res_banknifty.winning_trades}")
    print(f"losses:               {res_banknifty.losing_trades}")
    print(f"win rate:             {res_banknifty.accuracy_pct:.2f}%")
    print(f"gross P&L:            ₹{gross_banknifty:.2f}")
    print(f"charges:              ₹{res_banknifty.total_charges:.2f}")
    print(f"slippage:             ₹{slip_banknifty:.2f}")
    print(f"net P&L:              ₹{res_banknifty.net_profit:.2f}")
    print(f"max drawdown:         {res_banknifty.max_drawdown_pct:.2f}%")

    # -------------------------------------------------------------
    # TEST 3 — SENSEX ONLY
    # -------------------------------------------------------------
    res_sensex = run_engine_on_symbols(["SENSEX"])
    gross_sensex = sum(t.get("gross_pnl", 0) for t in res_sensex.trade_log)
    slip_sensex = sum(t.get("slippage", 0) for t in res_sensex.trade_log)

    print("\n============================================================")
    print("TEST 3 — SENSEX ONLY")
    print("============================================================")
    print(f"candles:              {res_sensex.total_candles_scanned}")
    print(f"signals:              {res_sensex.signals_generated}")
    print(f"trades:               {res_sensex.trades_taken}")
    print(f"wins:                 {res_sensex.winning_trades}")
    print(f"losses:               {res_sensex.losing_trades}")
    print(f"win rate:             {res_sensex.accuracy_pct:.2f}%")
    print(f"gross P&L:            ₹{gross_sensex:.2f}")
    print(f"charges:              ₹{res_sensex.total_charges:.2f}")
    print(f"slippage:             ₹{slip_sensex:.2f}")
    print(f"net P&L:              ₹{res_sensex.net_profit:.2f}")
    print(f"max drawdown:         {res_sensex.max_drawdown_pct:.2f}%")

    # -------------------------------------------------------------
    # TEST 4 — SIX SYMBOL COMBINED
    # -------------------------------------------------------------
    six_symbols = ["NIFTY50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"]
    res_combined = run_engine_on_symbols(six_symbols)
    gross_combined = sum(t.get("gross_pnl", 0) for t in res_combined.trade_log)
    slip_combined = sum(t.get("slippage", 0) for t in res_combined.trade_log)

    print("\n============================================================")
    print("TEST 4 — SIX SYMBOL COMBINED")
    print("============================================================")
    print(f"portfolio candles:    {res_combined.total_candles_scanned}")
    print(f"portfolio signals:    {res_combined.signals_generated}")
    print(f"portfolio trades:     {res_combined.trades_taken}")
    print(f"wins:                 {res_combined.winning_trades}")
    print(f"losses:               {res_combined.losing_trades}")
    print(f"win rate:             {res_combined.accuracy_pct:.2f}%")
    print(f"gross P&L:            ₹{gross_combined:.2f}")
    print(f"charges:              ₹{res_combined.total_charges:.2f}")
    print(f"slippage:             ₹{slip_combined:.2f}")
    print(f"net P&L:              ₹{res_combined.net_profit:.2f}")
    print(f"max drawdown:         {res_combined.max_drawdown_pct:.2f}%")
    print(f"max simultaneous:     {res_combined.portfolio_summary.get('max_simultaneous_positions', 0)}")

    print("\n=== symbol_summary for every six symbols ===")
    for sym, s_data in res_combined.symbol_summary.items():
        print(f"{sym}: candles={s_data['candles']}, signals={s_data['signals']}, trades={s_data['trades']}, wins={s_data['wins']}, losses={s_data['losses']}, win_rate={s_data['win_rate']:.2f}%, net_pnl=₹{s_data['net_pnl']:.2f}, charges=₹{s_data['charges']:.2f}")

    # -------------------------------------------------------------
    # CRITICAL VALIDATION CHECKS
    # -------------------------------------------------------------
    print("\n============================================================")
    print("CRITICAL VALIDATION")
    print("============================================================")

    # 1. Combined trade_log contains trades from multiple symbols
    symbols_in_trade_log = set(t.get("underlying", t.get("symbol")) for t in res_combined.trade_log)
    print(f"1. Unique symbols in combined trade_log: {symbols_in_trade_log}")
    assert len(symbols_in_trade_log) > 1, f"FAIL: only {len(symbols_in_trade_log)} symbol in trade log"

    # 2 & 3. Count trades by symbol & Print
    sym_counts = {}
    for sym in six_symbols:
        count = sum(1 for t in res_combined.trade_log if t.get("underlying", t.get("symbol")) == sym)
        sym_counts[sym] = count
        print(f"{sym}: {count} trades")

    # 4. Verify sum(symbol trade counts) == portfolio total trades
    sum_trade_counts = sum(sym_counts.values())
    print(f"4. sum(symbol trade counts) [{sum_trade_counts}] == portfolio total trades [{res_combined.trades_taken}]: {sum_trade_counts == res_combined.trades_taken}")
    assert sum_trade_counts == res_combined.trades_taken

    # 5. Verify sum(symbol net P&L) == portfolio net P&L
    sum_symbol_pnl = sum(res_combined.symbol_summary[s]["net_pnl"] for s in six_symbols)
    print(f"5. sum(symbol net P&L) [₹{sum_symbol_pnl:.2f}] == portfolio net P&L [₹{res_combined.net_profit:.2f}]: {abs(sum_symbol_pnl - res_combined.net_profit) < 0.1}")
    assert abs(sum_symbol_pnl - res_combined.net_profit) < 0.1

    # 6. Verify sum(symbol charges) == portfolio charges
    sum_symbol_charges = sum(res_combined.symbol_summary[s]["charges"] for s in six_symbols)
    print(f"6. sum(symbol charges) [₹{sum_symbol_charges:.2f}] == portfolio charges [₹{res_combined.total_charges:.2f}]: {abs(sum_symbol_charges - res_combined.total_charges) < 0.1}")
    assert abs(sum_symbol_charges - res_combined.total_charges) < 0.1

    # 7. Verify winning trades + losing trades == total trades
    print(f"7. winning trades ({res_combined.winning_trades}) + losing trades ({res_combined.losing_trades}) == total ({res_combined.trades_taken}): {res_combined.winning_trades + res_combined.losing_trades == res_combined.trades_taken}")
    assert res_combined.winning_trades + res_combined.losing_trades == res_combined.trades_taken

    # 8. Verify len(trade_log) == total trades
    print(f"8. len(trade_log) ({len(res_combined.trade_log)}) == total trades ({res_combined.trades_taken}): {len(res_combined.trade_log) == res_combined.trades_taken}")
    assert len(res_combined.trade_log) == res_combined.trades_taken

    # 9. Verify CSV trade count == BacktestResult trade count
    task = BacktestTask(task_id="validation_test", status=STATUS_COMPLETED, result=res_combined.to_dict())
    csv_path = task.generate_csv()
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        lines = list(reader)
    # Find trade log header
    trade_start_idx = None
    for idx, row in enumerate(lines):
        if row and row[0] == "timestamp":
            trade_start_idx = idx + 1
            break
    csv_trade_count = len(lines) - trade_start_idx if trade_start_idx is not None else 0
    print(f"9. CSV trade count ({csv_trade_count}) == BacktestResult trade count ({res_combined.trades_taken}): {csv_trade_count == res_combined.trades_taken}")
    assert csv_trade_count == res_combined.trades_taken

    # -------------------------------------------------------------
    # CHRONOLOGICAL VALIDATION
    # -------------------------------------------------------------
    print("\n============================================================")
    print("CHRONOLOGICAL VALIDATION")
    print("============================================================")
    timestamps = [t["entry_time"] for t in res_combined.trade_log]
    is_sorted = all(timestamps[i] <= timestamps[i+1] for i in range(len(timestamps)-1))
    print(f"Trade timestamps globally sorted: {is_sorted}")
    assert is_sorted, "FAIL: Trade timestamps are not globally sorted!"

    print("\nFirst 20 trades:")
    print(f"{'Index':<6} {'Timestamp':<26} {'Symbol':<12} {'Entry Price':<14} {'Exit Price':<14} {'P&L (₹)':<10}")
    print("-" * 84)
    for idx, t in enumerate(res_combined.trade_log[:20]):
        sym = t.get("underlying", t.get("symbol"))
        print(f"{idx+1:<6} {t['entry_time']:<26} {sym:<12} {t['entry_price']:<14.2f} {t['exit_price']:<14.2f} {t['net_pnl']:<10.2f}")

    print("\nLast 20 trades:")
    print(f"{'Index':<6} {'Timestamp':<26} {'Symbol':<12} {'Entry Price':<14} {'Exit Price':<14} {'P&L (₹)':<10}")
    print("-" * 84)
    start_idx = max(0, len(res_combined.trade_log) - 20)
    for idx, t in enumerate(res_combined.trade_log[start_idx:]):
        sym = t.get("underlying", t.get("symbol"))
        print(f"{start_idx+idx+1:<6} {t['entry_time']:<26} {sym:<12} {t['entry_price']:<14.2f} {t['exit_price']:<14.2f} {t['net_pnl']:<10.2f}")

    with open("/tmp/res_combined.pkl", "wb") as f:
        pickle.dump(res_combined, f)

    # -------------------------------------------------------------
    # SYMBOL-ORDER INVARIANCE
    # -------------------------------------------------------------
    if "--portfolio" not in sys.argv and "-p" not in sys.argv:
        print("\n============================================================")
        print("SYMBOL-ORDER INVARIANCE")
        print("============================================================")
        order_b = ["BANKEX", "SENSEX", "MIDCPNIFTY", "FINNIFTY", "BANKNIFTY", "NIFTY50"]

        print("Run A uses combined run result (exact same order and parameters)...")
        res_a = res_combined
        print("Evaluating Run B with reversed symbol order...")
        res_b = run_engine_on_symbols(order_b)

        print(f"Run A total trades: {res_a.trades_taken}, Run B total trades: {res_b.trades_taken}")
        print(f"Run A net P&L:      ₹{res_a.net_profit:.2f}, Run B net P&L:      ₹{res_b.net_profit:.2f}")
        print(f"Run A charges:      ₹{res_a.total_charges:.2f}, Run B charges:      ₹{res_b.total_charges:.2f}")
        print(f"Run A drawdown:     {res_a.max_drawdown_pct:.2f}%, Run B drawdown:     {res_b.max_drawdown_pct:.2f}%")

        assert res_a.trades_taken == res_b.trades_taken, "Order variance: trades_taken differ"
        assert abs(res_a.net_profit - res_b.net_profit) < 0.01, "Order variance: net_profit differs"
        assert abs(res_a.total_charges - res_b.total_charges) < 0.01, "Order variance: total_charges differ"
        assert abs(res_a.max_drawdown_pct - res_b.max_drawdown_pct) < 0.01, "Order variance: max_drawdown differs"

        for i in range(len(res_a.trade_log)):
            t_a = res_a.trade_log[i]
            t_b = res_b.trade_log[i]
            assert t_a["entry_time"] == t_b["entry_time"], f"Trade {i} entry_time mismatch: {t_a['entry_time']} vs {t_b['entry_time']}"
            assert t_a["symbol"] == t_b["symbol"], f"Trade {i} symbol mismatch: {t_a['symbol']} vs {t_b['symbol']}"
            assert t_a["entry_price"] == t_b["entry_price"], f"Trade {i} entry_price mismatch"
            assert t_a["exit_price"] == t_b["exit_price"], f"Trade {i} exit_price mismatch"
            assert t_a["net_pnl"] == t_b["net_pnl"], f"Trade {i} net_pnl mismatch"

        print("Order invariance verified: Run A and Run B are 100% IDENTICAL.")
    else:
        print("\n(Skipping invariance in --portfolio mode; cached to /tmp/res_combined.pkl for --invariance step)")

    # -------------------------------------------------------------
    # INDIVIDUAL VS COMBINED SYMBOL COMPARISON
    # -------------------------------------------------------------
    print("\n============================================================")
    print("INDIVIDUAL VS COMBINED SYMBOL CHECK (BENCHMARKED INDICES)")
    print("============================================================")
    print(f"{'Symbol':<12} {'Individual Trades':<18} {'Combined Trades':<18} {'Difference':<12}")
    print("-" * 62)
    benchmarked = [
        ("NIFTY50", res_nifty.trades_taken),
        ("BANKNIFTY", res_banknifty.trades_taken),
        ("SENSEX", res_sensex.trades_taken),
    ]
    for sym, ind_t in benchmarked:
        comb_t = res_combined.symbol_summary[sym]["trades"]
        diff = comb_t - ind_t
        print(f"{sym:<12} {ind_t:<18} {comb_t:<18} {diff:<12}")

    # -------------------------------------------------------------
    # REJECTION REASONS BREAKDOWN
    # -------------------------------------------------------------
    print("\n============================================================")
    print("SIGNAL REJECTION CLASSIFICATION & NO SILENT LOSS")
    print("============================================================")
    print(f"Total rejected signals across combined run: {res_combined.rejected_signals_total_count}")
    print(f"Total risk rejections:                      {res_combined.risk_rejections}")
    print("Top rejection reasons:")
    for reason, count in sorted(res_combined.rejection_reason_counts.items(), key=lambda x: x[1], reverse=True)[:15]:
        print(f"  - [{count} times] {reason}")


if __name__ == "__main__":
    main()

