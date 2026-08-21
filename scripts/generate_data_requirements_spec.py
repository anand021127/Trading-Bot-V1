"""Generates docs/V8D_HISTORICAL_OPTION_DATA_REQUIREMENTS.md."""
import csv
import json
import os
from collections import defaultdict

def generate_spec():
    with open("strategy_v8_execution_research.csv", "r") as f:
        r = csv.DictReader(f)
        v8d = [row for row in r if row["variant"] == "V8-D"]

    v8d.sort(key=lambda x: x["entry_timestamp"])

    contracts = {}
    for t in v8d:
        u = t["underlying"]
        exp = t["expiry"]
        stk = float(t["strike"])
        opt = t["option_type"]
        key = f"{u}_{exp}_{int(stk)}_{opt}"
        if key not in contracts:
            contracts[key] = {
                "underlying": u,
                "expiry": exp,
                "strike": stk,
                "option_type": opt,
                "first_trade_timestamp": t["entry_timestamp"],
                "last_trade_timestamp": t["exit_timestamp"],
                "trade_count": 0,
            }
        contracts[key]["trade_count"] += 1
        if t["exit_timestamp"] > contracts[key]["last_trade_timestamp"]:
            contracts[key]["last_trade_timestamp"] = t["exit_timestamp"]

    sorted_contracts = sorted(
        contracts.values(),
        key=lambda x: (x["underlying"], x["expiry"], x["strike"], x["option_type"])
    )

    md_lines = []
    md_lines.append("# Strategy V8-D Historical Option Data Requirements Specification\n")
    md_lines.append("## 1. Executive Summary of Requirements")
    md_lines.append("- **Strategy Identifier:** Strategy V8-D (Fixed -20% Option Stop, +15% Target, ATM Strike Selection)")
    md_lines.append("- **Underlyings Required (2):** `NIFTY50`, `BANKNIFTY`")
    md_lines.append("- **Historical Date Range:** `2024-01-01` to `2024-11-06` (Trading days only, 09:15 to 15:30 IST)")
    md_lines.append("- **Total Unique Historical Expiries Required (90):** 45 NIFTY50 weekly expiries + 45 BANKNIFTY weekly expiries")
    md_lines.append("- **Total Unique Option Strikes Required (160):** 79 NIFTY50 strikes (21,300 to 26,200) + 81 BANKNIFTY strikes (44,600 to 54,200)")
    md_lines.append("- **Option Types:** Both Calls (`CE`) and Puts (`PE`)")
    md_lines.append("- **Total Unique Historical Option Contracts:** **465 contracts** (223 NIFTY50 contracts + 242 BANKNIFTY contracts)")
    md_lines.append("- **Required Candle Resolution:** **1-minute OHLCV** (recommended for intrabar stop/target execution precision) or **5-minute OHLCV** (matching signal timeframe)")
    md_lines.append("- **Volume Data:** **MANDATORY** (Used for volume/liquidity validation and realistic slippage modeling)")
    md_lines.append("- **Open Interest (OI) Data:** **OPTIONAL / RECOMMENDED**")
    md_lines.append("- **Expected Data Directory:** `real_data/options_cache/`\n")

    md_lines.append("---")
    md_lines.append("## 2. Exact Schema Expected by OptionsDataLayer\n")
    md_lines.append("The repository's `backend.backtest.options_data_layer.HistoricalOptionsDataLoader` automatically loads files placed inside `real_data/options_cache/` matching either the Single-Contract format or Array format.\n")
    
    sample_json = {
        "contract": {
            "underlying": "NIFTY50",
            "expiry": "2024-07-04",
            "strike": 24050.0,
            "option_type": "CE",
            "instrument_key": "NSE_FO|NIFTY2470424050CE",
            "lot_size": 25
        },
        "candles": [
            {
                "timestamp": "2024-07-01T09:15:00+05:30",
                "open": 120.50,
                "high": 125.00,
                "low": 118.00,
                "close": 122.30,
                "volume": 145000,
                "oi": 850000
            }
        ]
    }
    md_lines.append("```json")
    md_lines.append(json.dumps(sample_json, indent=2))
    md_lines.append("```\n")

    md_lines.append("### Field Definitions:")
    md_lines.append("| Field | Type | Description | Required |")
    md_lines.append("| :--- | :--- | :--- | :---: |")
    md_lines.append("| `timestamp` | ISO-8601 String | Candle start time (e.g. `2024-07-01T09:15:00+05:30`) | **YES** |")
    md_lines.append("| `open` | Float | Opening price of the option premium in INR | **YES** |")
    md_lines.append("| `high` | Float | High price of the option premium in INR | **YES** |")
    md_lines.append("| `low` | Float | Low price of the option premium in INR | **YES** |")
    md_lines.append("| `close` | Float | Closing price of the option premium in INR | **YES** |")
    md_lines.append("| `volume` | Float / Int | Traded volume during the bar interval | **YES** |")
    md_lines.append("| `oi` | Float / Int / Null | Open interest at bar close | OPTIONAL |")

    md_lines.append("\n---")
    md_lines.append("## 3. Code Readiness & OptionsDataLayer Integration")
    md_lines.append("1. **Can OptionsDataLayer consume real historical option OHLC without code changes?**")
    md_lines.append("   - **YES.** `HistoricalOptionsDataLoader` in `backend/backtest/options_data_layer.py` already includes `load_from_directory()` and O(1) indexed lookup tables mapping `(contract_key, timestamp) -> HistoricalOptionRecord`.")
    md_lines.append("2. **Code changes required to replace Black-76 simulated premiums:**")
    md_lines.append("   - None in the strategy logic (`V8DStrategy`). In the backtest runner, passing `options_data_loader=loader` automatically switches `BacktestEngine` into strict real historical options execution mode.")
    md_lines.append("3. **Backtest behavior when historical option data is missing:**")
    md_lines.append("   - When real option data is missing for any bar or contract, `OptionsDataLayer` strictly returns `None`. `BacktestEngine` records the signal under `DATA_UNAVAILABLE` and skips the trade completely, preventing synthetic price fallbacks or spot price substitutions.")

    md_lines.append("\n---")
    md_lines.append("## 4. Complete List of All 465 Unique Historical Option Contracts Required\n")
    md_lines.append("| # | Underlying | Expiry Date | Strike | Type | First Trade Window | Last Trade Window | Total Trades |")
    md_lines.append("| :--- | :--- | :--- | :--- | :---: | :--- | :--- | :---: |")

    for idx, c in enumerate(sorted_contracts, start=1):
        md_lines.append(
            f"| {idx} | {c['underlying']} | {c['expiry']} | {int(c['strike'])} | {c['option_type']} | {c['first_trade_timestamp'][:16]} | {c['last_trade_timestamp'][:16]} | {c['trade_count']} |"
        )

    os.makedirs("docs", exist_ok=True)
    with open("docs/V8D_HISTORICAL_OPTION_DATA_REQUIREMENTS.md", "w") as f_out:
        f_out.write("\n".join(md_lines) + "\n")
    print(f"Generated specification with {len(sorted_contracts)} contracts.")

if __name__ == "__main__":
    generate_spec()
