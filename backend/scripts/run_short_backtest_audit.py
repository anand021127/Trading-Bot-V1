"""Script to run 2024-01-01 to 2024-01-10 NIFTY50 backtest and print granular diagnostics."""
import json
import logging
import os
from pprint import pprint

from backend.backtest.engine import BacktestEngine
from backend.backtest.historical_data_io import load_dataset_safe
from backend.backtest.options_data_layer import HistoricalOptionsDataLoader
from backend.indicators.ema import calculate_ema
from backend.indicators.choppiness import choppiness_index

logging.basicConfig(level=logging.INFO)

def build_trend_series(candles, ema_fast=20, ema_slow=50, ci_period=14):
    if len(candles) < ema_slow:
        return {}
    closes = [float(c["close"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    ema_fast_vals = calculate_ema(closes, ema_fast)
    ema_slow_vals = calculate_ema(closes, ema_slow)
    ci_vals = choppiness_index(highs, lows, closes, ci_period)
    ci_offset = len(closes) - len(ci_vals)
    series = {}
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

def main():
    print("=" * 70)
    print("RUNNING HISTORICAL OPTIONS BACKTEST AUDIT (2024-01-01 to 2024-01-10)")
    print("=" * 70)

    start_date = "2024-01-01"
    end_date = "2024-01-10"
    symbol = "NIFTY50"

    real_data_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "real_data",
        f"{symbol}_2024_5min.json",
    )
    all_candles = load_dataset_safe(real_data_path, auto_repair=True)
    candles = [
        c for c in all_candles
        if start_date <= c.get("timestamp", "")[:10] <= end_date
    ]
    print(f"Loaded {len(candles)} candles for {symbol} ({start_date} to {end_date}) from {real_data_path}")

    # Build trend series
    trend_series = build_trend_series(candles)

    option_contexts = {
        symbol: {
            "underlying_trend_series": trend_series,
        }
    }

    loader = HistoricalOptionsDataLoader()
    engine = BacktestEngine(capital=100000.0, min_candles_required=60)

    result = engine.run(
        symbol_candles={symbol: candles},
        strategy_names=["OPTION_PREMIUM"],
        option_contexts=option_contexts,
        options_data_loader=loader,
        require_real_options=True,
    )

    print("\n" + "=" * 70)
    print("DIAGNOSTIC REPORT:")
    print("=" * 70)
    report = {
        "candles_loaded": result.candles_loaded,
        "warmup_bars": result.warmup_bars,
        "candles_evaluated": result.candles_evaluated,
        "underlying_trends": {
            "bullish": result.trend_bullish,
            "bearish": result.trend_bearish,
            "neutral": result.trend_neutral,
        },
        "directional_signals": result.directional_signals,
        "intents": {
            "ce_intents": result.ce_intents,
            "pe_intents": result.pe_intents,
        },
        "contract_resolutions": {
            "attempts": result.contract_resolution_attempts,
            "resolved": result.contracts_resolved,
            "failures": result.contract_resolution_failures,
            "failures_breakdown": result.contract_resolution_failures_breakdown,
        },
        "option_premiums": {
            "lookup_attempts": result.option_premium_lookup_attempts,
            "found": result.option_premiums_found,
            "missing": result.option_premium_missing,
        },
        "risk_rejections": {
            "total": result.risk_rejections,
            "breakdown": result.risk_rejections_breakdown,
        },
        "execution": {
            "orders_created": result.orders_created,
            "trades_opened": result.trades_opened,
            "trades_closed": result.trades_closed,
            "total_trades_taken": result.trades_taken,
            "winning_trades": result.winning_trades,
            "losing_trades": result.losing_trades,
            "net_profit": result.net_profit,
            "net_profit_pct": result.net_profit_pct,
            "accuracy_pct": result.accuracy_pct,
        },
        "rejection_reason_counts": result.rejection_reason_counts,
    }
    print(json.dumps(report, indent=2))

if __name__ == "__main__":
    main()
