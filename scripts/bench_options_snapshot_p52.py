"""PHASE 5.2 §16/§17 — profile build_option_chain_snapshot (the per-bar
backtest hot path) before and after optimization.

The loader is populated with a clearly-labeled BENCHMARK FIXTURE contract
(generated OHLCV, never used as production market data) purely to measure
the engine's cost curve. Result values are compared before/after to prove
the optimization changes NOTHING (§17: old result == new result).
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.abspath("."))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.backtest.options_data_layer import HistoricalOptionsDataLoader


def fixture_contract(n_bars: int = 4000) -> list:
    """Benchmark fixture (NOT production data): 4000 5-minute option candles
    ≈ 8 trading days, drifting price, deterministic."""
    out = []
    t0 = datetime(2024, 1, 1, 9, 15)
    px = 100.0
    for i in range(n_bars):
        px += 0.15 - (i % 7) * 0.04
        ts = (t0 + timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M:%S")
        out.append({
            "timestamp": ts,
            "open": round(px - 0.2, 2), "high": round(px + 0.5, 2),
            "low": round(px - 0.5, 2), "close": round(px, 2),
            "volume": 1000 + (i % 100) * 7,
        })
    return out


def main() -> None:
    loader = HistoricalOptionsDataLoader(auto_load_cache=False)
    candles = fixture_contract()
    loader.load_contract_candles(
        underlying="NIFTY50", expiry="2024-01-11", strike=24000.0,
        option_type="CE", instrument_key="BENCH_FIXTURE_NIFTY_CE",
        candles=candles, lot_size=75,
    )
    loader.load_contract_candles(
        underlying="NIFTY50", expiry="2024-01-11", strike=24000.0,
        option_type="PE", instrument_key="BENCH_FIXTURE_NIFTY_PE",
        candles=candles, lot_size=75,
    )

    spot = 24020.0
    # Simulate one backtest day: 75 bars, one snapshot per bar (as engine does)
    timestamps = [c["timestamp"] for c in candles[:75]]

    t0 = time.perf_counter()
    chains = []
    for ts in timestamps:
        chains.append(loader.build_option_chain_snapshot(
            underlying="NIFTY50", target_date=date(2024, 1, 1),
            spot_price=spot, timestamp=ts, exact_atm_only=True,
        ))
    dt = time.perf_counter() - t0

    # Equivalence capture: dump the FULL snapshot series so the optimized
    # implementation can be diffed byte-for-byte against this baseline.
    tag = os.environ.get("BENCH_TAG", "before")
    with open(f"analysis/bench_options_snapshot_{tag}_chains.json", "w", encoding="utf-8") as f:
        json.dump(chains, f, indent=1, sort_keys=True)

    print(f"75 bars, 2 contracts x {len(candles)} fixture bars each:")
    print(f"  total: {dt:.3f}s  per-bar: {dt/75*1000:.1f} ms  "
          f"(25d backtest ≈ {dt/75*1650:.1f}s, 1 year ≈ {dt/75*1650*12:.0f}s)")
    nonempty = [c for c in chains if c]
    print(f"  non-empty snapshots: {len(nonempty)}/75")
    if nonempty:
        print(f"  sample: {json.dumps(nonempty[0][0])[:160]}")

    out = {
        "benchmark": "PHASE 5.2 §16 options snapshot hot path (fixture loader, labeled BENCH_FIXTURE)",
        "bars": len(timestamps),
        "fixture_bars_per_contract": len(candles),
        "total_seconds": round(dt, 3),
        "per_bar_ms": round(dt / 75 * 1000, 2),
        "first_snapshot": nonempty[0][0] if nonempty else None,
    }
    os.makedirs("analysis", exist_ok=True)
    with open("analysis/bench_options_snapshot_before.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("saved analysis/bench_options_snapshot_before.json")


if __name__ == "__main__":
    main()
