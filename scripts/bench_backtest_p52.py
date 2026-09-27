"""PHASE 5.2 §16 — staged backtest benchmark (1d / 5d / 25d).

Measures each stage separately with the PRODUCTION strategy (V8_D), the
production calendar/loader, and REAL historical data (real_data/
NIFTY50_2024_5min.json + real_data/options_cache contract candles).
No synthetic data. Results go to analysis/bench_phase52.json.
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

SYMBOL = "NIFTY50"
DATA = "real_data/NIFTY50_2024_5min.json"


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
    out = [c for c in candles if start.isoformat() <= c["timestamp"][:10] < end.isoformat()]
    return out


def main() -> None:
    t_all = time.perf_counter()
    stages: dict = {}

    # Stage 1: load
    t0 = time.perf_counter()
    all_candles = load_dataset_safe(DATA, auto_repair=True)
    stages["data_loading_seconds"] = round(time.perf_counter() - t0, 3)
    print(f"loaded {len(all_candles)} candles in {stages['data_loading_seconds']}s")

    # First 25 trading days of 2024 with enough history for warmup:
    # we slice from a date that already has 60+ bars of history in front of
    # it (engine requires min_candles_required before evaluating).
    first_ts = all_candles[0]["timestamp"][:10]
    start = date.fromisoformat(first_ts)
    # skip ahead to have warmup history available in the slices themselves
    start = start + timedelta(days=10)

    # Stage 2: trend series over the FULL year (production approach)
    t0 = time.perf_counter()
    full_series = build_trend_series(all_candles)
    stages["trend_series_seconds"] = round(time.perf_counter() - t0, 3)

    # Stage 3: options cache load (real cached contracts)
    t0 = time.perf_counter()
    loader = HistoricalOptionsDataLoader(auto_load_cache=True)
    stages["options_cache_load_seconds"] = round(time.perf_counter() - t0, 3)
    stages["options_cache_contracts"] = loader.available_contracts_count()
    stages["options_cache_candles"] = loader.available_candles_count()
    print(f"options cache: {stages['options_cache_contracts']} contracts, "
          f"{stages['options_cache_candles']} candles")

    engine = BacktestEngine(
        strategy_engine=MultiStrategyEngine([V8DStrategy()]),
        costs=CostConfig(),
        capital=100000.0,
        risk_pct_per_trade=0.025,
        ai_mode="disabled",
    )

    results = {}
    for days in (1, 5, 25):
        candles = slice_days(all_candles, start, days)
        if len(candles) < 80:
            print(f"skip {days}d: only {len(candles)} candles")
            continue
        option_contexts = {SYMBOL: {"underlying_trend_series": full_series}}
        t0 = time.perf_counter()
        result = engine.run(
            {SYMBOL: candles},
            strategy_names=["V8_D_PULLBACK_ATM"],
            option_contexts=option_contexts,
            options_data_loader=loader,
            require_real_options=True,
        )
        secs = round(time.perf_counter() - t0, 2)
        results[f"{days}d"] = {
            "bars": len(candles),
            "seconds": secs,
            "bars_per_second": round(len(candles) / secs, 1) if secs > 0 else None,
            "trades": result.trades_taken,
            "signals": result.directional_signals,
            "net_pnl": result.portfolio_summary.get("net_profit"),
            "candles_evaluated": result.candles_evaluated,
            "warmup_bars": result.warmup_bars,
            "ai_backtest_status": result.ai_backtest_status,
            "rejections_total": result.rejected_signals_total_count,
        }
        print(f"{days}d: {secs}s, bars={len(candles)}, trades={result.trades_taken}, "
              f"signals={result.directional_signals}, ai_status={result.ai_backtest_status}")

    out = {
        "benchmark": "PHASE 5.2 §16 staged backtest benchmark",
        "date": "2026-09-26",
        "symbol": SYMBOL, "data": DATA, "strategy": "V8_D_PULLBACK_ATM",
        "stages": stages,
        "results": results,
        "note": "Real historical data only. Same underlying/timeframe/contract/strategy "
                "across runs. ai_backtest_status honestly AI_BACKTEST_UNAVAILABLE.",
    }
    os.makedirs("analysis", exist_ok=True)
    with open("analysis/bench_phase52.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("\nsaved analysis/bench_phase52.json")
    print("total wall:", round(time.perf_counter() - t_all, 1), "s")


if __name__ == "__main__":
    main()
