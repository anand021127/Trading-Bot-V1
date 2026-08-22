import sys
import os
sys.path.insert(0, os.path.abspath("."))
import json
import glob
import csv
from datetime import datetime
from backend.indicators.ema import ema as calculate_ema
from backend.indicators.choppiness import choppiness_index
from backend.backtest.engine import BacktestEngine, CostConfig, BacktestTrade
from backend.strategy.strategy_engine import MultiStrategyEngine
from backend.strategy.strategies.ema_trend import EMATrendStrategy
from backend.strategy.strategies.option_premium import OptionPremiumStrategy
from backend.backtest.historical_contract_resolver import (
    get_nearest_expiry_for_date,
    build_trading_symbol,
    _EXCHANGE_SEGMENT,
    DataQualityReport,
)

from backend.backtest.historical_data_io import load_dataset_safe

def load_candles(path):
    return load_dataset_safe(path, auto_repair=True)

def build_trend_series(candles, ema_fast=20, ema_slow=50, ci_period=14):
    if len(candles) < ema_slow:
        return {}
    closes = [float(c['close']) for c in candles]
    highs = [float(c['high']) for c in candles]
    lows = [float(c['low']) for c in candles]
    ema_fast_vals = calculate_ema(closes, ema_fast)
    ema_slow_vals = calculate_ema(closes, ema_slow)
    ci_vals = choppiness_index(highs, lows, closes, ci_period)
    ci_offset = len(closes) - len(ci_vals)
    series = {}
    for i in range(ema_slow - 1, len(closes)):
        ts = candles[i].get('timestamp')
        if not ts:
            continue
        ci_idx = i - ci_offset
        if 0 <= ci_idx < len(ci_vals) and ci_vals[ci_idx] > 61.8:
            series[ts] = 'NEUTRAL'
        elif ema_fast_vals[i] > ema_slow_vals[i] and closes[i] > ema_fast_vals[i]:
            series[ts] = 'BULLISH'
        elif ema_fast_vals[i] < ema_slow_vals[i]:
            series[ts] = 'BEARISH'
        else:
            series[ts] = 'NEUTRAL'
    return series

def main():
    print("Starting REAL Backtest evaluation...")
    data_files = sorted(glob.glob("real_data/*.json"))
    symbol_candles = {}
    option_contexts = {}

    for path in data_files:
        sym = os.path.basename(path).replace("_2024_5min.json", "")
        candles = load_candles(path)
        symbol_candles[sym] = candles
        trend_series = build_trend_series(candles)
        option_contexts[sym] = {"underlying_trend_series": trend_series}
        print(f"Loaded {sym}: {len(candles)} candles ({candles[0]['timestamp']} to {candles[-1]['timestamp']})")

    # Run Backtest with EMA_TREND strategy
    engine = BacktestEngine(
        strategy_engine=MultiStrategyEngine([EMATrendStrategy()]),
        costs=CostConfig(),
        capital=100000.0,
        risk_pct_per_trade=0.01,
    )

    print("Executing backtest across all symbols...")
    start_time = datetime.now()
    result = engine.run(symbol_candles, strategy_names=["EMA_TREND"], option_contexts=option_contexts)
    duration = (datetime.now() - start_time).total_seconds()
    print(f"Backtest completed in {duration:.2f} seconds.")

    # Save detailed trade CSV
    csv_file = "real_backtest_trade_log.csv"
    with open(csv_file, "w", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow([
            "date", "entry_time", "exit_time", "underlying", "expiry", "strike",
            "option_type", "instrument_key", "entry_price", "exit_price", "quantity",
            "setup_score", "confidence", "stop_loss", "target", "exit_reason",
            "gross_pnl", "brokerage", "stt", "exchange_charges", "sebi_charges",
            "gst", "stamp_duty", "slippage", "total_cost", "net_pnl"
        ])
        
        # Enrich trades with contract info if needed
        for t in result.trade_log:
            entry_ts = t.get("entry_time", "")
            date_str = entry_ts[:10] if entry_ts else ""
            underlying = t.get("symbol", "")
            entry_dt = datetime.fromisoformat(entry_ts.replace("Z", "+00:00")).date() if entry_ts else None
            expiry_str = ""
            instrument_key = f"NSE_INDEX|{underlying}"
            option_type = "INDEX_SPOT"
            strike = ""
            
            if entry_dt:
                expiry = get_nearest_expiry_for_date(underlying, entry_dt)
                expiry_str = expiry.isoformat()
                strike = round(t.get("entry_price", 0) / 50.0) * 50.0

            writer.writerow([
                date_str,
                t.get("entry_time", ""),
                t.get("exit_time", ""),
                underlying,
                expiry_str,
                strike,
                option_type,
                instrument_key,
                t.get("entry_price", 0),
                t.get("exit_price", 0),
                t.get("quantity", 0),
                t.get("confidence", 0),
                t.get("confidence", 0),
                t.get("stop_loss", ""),
                t.get("target", ""),
                t.get("exit_reason", ""),
                t.get("gross_pnl", 0),
                t.get("brokerage", 0),
                t.get("stt", 0),
                t.get("exchange_charges", 0),
                t.get("sebi_charges", 0),
                t.get("gst", 0),
                t.get("stamp_duty", 0),
                t.get("slippage", 0),
                t.get("total_cost", 0),
                t.get("net_pnl", 0)
            ])

    # Save summary JSON
    summary_file = "real_backtest_summary.json"
    with open(summary_file, "w") as fp:
        json.dump(result.to_dict(), fp, indent=2)

    print("Saved results to real_backtest_trade_log.csv and real_backtest_summary.json")

if __name__ == "__main__":
    main()
