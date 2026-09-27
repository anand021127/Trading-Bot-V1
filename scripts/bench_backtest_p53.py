"""PHASE 5.3 §28 — staged backtest benchmark incl. BANKEX (1d / 5d / 25d).

Runs the PRODUCTION strategy (V8-D, untouched parameters) over REAL
historical underlying candles (real_data/<SYMBOL>_2024_5min.json) for
NIFTY50 and BANKEX. Honest data policy:

  * underlying candles: real exchange data from the repo dataset
  * option candles: the local historical options cache is EMPTY on this
    machine, so require_real_options runs fail with BACKTEST_INCOMPLETE /
    FAILED_INCOMPLETE_COVERAGE rather than simulating premiums. We run
    BOTH modes and record both outcomes: the options-required failure is
    the honest evidence, the spot-only run demonstrates the BANKEX
    strategy/engine path end-to-end (labelled SPOT_ONLY, never presented
    as an options backtest).

Results -> analysis/bench_phase53_bankex.json
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.abspath("."))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("TRADING_MODE", "paper")
os.environ.setdefault("TRADING_STRATEGY", "V8_D_PULLBACK_ATM")
os.environ.setdefault("TRADING_BOT_OFFLINE_TESTS", "1")

from backend.backtest.engine import BacktestEngine, CostConfig
from backend.backtest.historical_data_io import load_dataset_safe
from backend.backtest.options_data_layer import HistoricalOptionsDataLoader
from backend.indicators.choppiness import choppiness_index
from backend.indicators.ema import ema as calculate_ema
from backend.strategy.strategy_engine import MultiStrategyEngine
from backend.strategy.strategies.v8d_strategy import V8DStrategy

RUNS = [("NIFTY50", "real_data/NIFTY50_2024_5min.json"),
        ("BANKEX", "real_data/BANKEX_2024_5min.json")]


def build_trend_series(candles, ema_fast=20, ema_slow=50, ci_period=14):
    if len(candles) < ema_slow:
        return {}
    closes = [float(c["close"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    ef = calculate_ema(closes, ema_fast)
    es = calculate_ema(closes, ema_slow)
    ci = choppiness_index(highs, lows, closes, ci_period)
    off = len(closes) - len(ci)
    series = {}
    for i in range(ema_slow - 1, len(closes)):
        ts = candles[i].get("timestamp")
        if not ts:
            continue
        ci_idx = i - off
        if 0 <= ci_idx < len(ci) and ci[ci_idx] > 61.8:
            series[ts] = "NEUTRAL"
        elif ef[i] > es[i] and closes[i] > ef[i]:
            series[ts] = "BULLISH"
        elif ef[i] < es[i]:
            series[ts] = "BEARISH"
        else:
            series[ts] = "NEUTRAL"
    return series


def slice_days(candles, start: date, days: int):
    end = start + timedelta(days=days)
    return [c for c in candles if start.isoformat() <= c["timestamp"][:10] < end.isoformat()]


def run_symbol(symbol: str, data_path: str) -> dict:
    stages: dict = {}
    t0 = time.perf_counter()
    all_candles = load_dataset_safe(data_path, auto_repair=True)
    stages["data_loading_seconds"] = round(time.perf_counter() - t0, 3)

    first_ts = all_candles[0]["timestamp"][:10]
    start = date.fromisoformat(first_ts) + timedelta(days=10)

    full_series = build_trend_series(all_candles)

    loader = HistoricalOptionsDataLoader(auto_load_cache=True)
    stages["options_cache_contracts"] = loader.available_contracts_count()
    stages["options_cache_candles"] = loader.available_candles_count()

    engine = BacktestEngine(
        strategy_engine=MultiStrategyEngine([V8DStrategy()]),
        costs=CostConfig(), capital=100000.0, risk_pct_per_trade=0.025,
        ai_mode="disabled",
    )
    option_contexts = {symbol: {"underlying_trend_series": full_series}}

    results = {}
    for days in (1, 5, 25):
        candles = slice_days(all_candles, start, days)
        if len(candles) < 80:
            continue
        row: dict = {"bars": len(candles)}

        # Mode A (honest options requirement): expected to FAIL here because
        # the local options cache is empty — the failure itself is evidence.
        t0 = time.perf_counter()
        try:
            res_opt = engine.run(
                {symbol: candles}, strategy_names=["V8_D_PULLBACK_ATM"],
                option_contexts=option_contexts, options_data_loader=loader,
                require_real_options=True)
            row["options_required"] = {
                "status": "RAN",
                "seconds": round(time.perf_counter() - t0, 3),
                "trades": res_opt.trades_taken,
                "net_pnl": res_opt.portfolio_summary.get("net_profit"),
                "coverage_status": res_opt.coverage_status,
            }
        except Exception as exc:  # noqa: BLE001 — expected: BACKTEST_INCOMPLETE
            row["options_required"] = {
                "status": f"REFUSED:{type(exc).__name__}",
                "seconds": round(time.perf_counter() - t0, 3),
                "detail": str(exc)[:300],
            }

        # Mode B (strategy-path demonstration, SPOT_ONLY — clearly labelled):
        t0 = time.perf_counter()
        res = engine.run(
            {symbol: candles}, strategy_names=["V8_D_PULLBACK_ATM"],
            option_contexts=option_contexts, options_data_loader=loader,
            require_real_options=False)
        secs = round(time.perf_counter() - t0, 3)
        row["spot_only_demo"] = {
            "data_mode": res.data_mode,
            "seconds": secs,
            "bars_per_second": round(len(candles) / max(secs, 0.001), 1),
            "trades": res.trades_taken,
            "signals": res.directional_signals,
            "net_pnl": res.portfolio_summary.get("net_profit"),
            "ai_backtest_status": res.ai_backtest_status,
            "rejections_total": res.rejected_signals_total_count,
            "note": "SPOT_ONLY path — strategy/engine correctness demo, NOT an options backtest",
        }
        results[f"{days}d"] = row
        print(f"{symbol} {days}d: bars={len(candles)} "
              f"opt={row['options_required']['status']} "
              f"spot_trades={row['spot_only_demo']['trades']}")
    return {"stages": stages, "results": results}


def main() -> None:
    out = {
        "benchmark": "PHASE 5.3 §28 — staged backtest benchmark incl. BANKEX",
        "date": "2026-09-27",
        "strategy": "V8_D_PULLBACK_ATM (parameters untouched)",
        "symbols": {},
        "honesty": {
            "ai_backtest": "AI_BACKTEST_UNAVAILABLE (unchanged, never fabricated)",
            "options_data": "local historical options cache is EMPTY — options-required "
                            "runs are refused/failed honestly; spot-only rows are "
                            "labelled demo and are NOT options backtests",
        },
    }
    for symbol, path in RUNS:
        print(f"=== {symbol} ===")
        out["symbols"][symbol] = run_symbol(symbol, path)
    os.makedirs("analysis", exist_ok=True)
    with open("analysis/bench_phase53_bankex.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("saved analysis/bench_phase53_bankex.json")


if __name__ == "__main__":
    main()
