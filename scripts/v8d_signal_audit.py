#!/usr/bin/env python3
"""Offline V8-D signal audit on REAL historical 5-minute candles (real_data/*.json).

Answers, with data instead of opinion: how often does a valid V8-D setup occur,
for each symbol, which condition is the binding one, and what does the RSI
off-by-one fix change. Read-only; places no orders; invents no data.

  python scripts/v8d_signal_audit.py            # human-readable table
  python scripts/v8d_signal_audit.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import time as dtime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.strategy.strategies import v8d_strategy as v8d  # noqa: E402
from backend.strategy.v8d_diagnostics import explain_pullback  # noqa: E402

SYMBOL_FILES = {
    "NIFTY50": "NIFTY50_2024_5min.json", "BANKNIFTY": "BANKNIFTY_2024_5min.json",
    "FINNIFTY": "FINNIFTY_2024_5min.json", "MIDCPNIFTY": "MIDCPNIFTY_2024_5min.json",
    "SENSEX": "SENSEX_2024_5min.json", "BANKEX": "BANKEX_2024_5min.json",
}
WINDOW = 120            # same context length the live scanner requests
ENTRY_START, ENTRY_END = dtime(9, 20), dtime(14, 45)


def load(symbol: str) -> List[Dict[str, Any]]:
    return json.loads((ROOT / "real_data" / SYMBOL_FILES[symbol]).read_text())


_CORRECTED_RSI = v8d.calculate_rsi   # captured before any patching


def lagged_rsi(values: List[float], period: int = 14) -> List[float]:
    """The ORIGINAL (buggy) RSI: its last element was the previous candle's RSI,
    i.e. exactly the corrected RSI computed WITHOUT the newest close."""
    return _CORRECTED_RSI(values[:-1], period) if len(values) > period + 1 else []


def audit_symbol(candles: List[Dict[str, Any]], *, rsi_fn: Optional[Callable] = None) -> Dict[str, Any]:
    strat = v8d.V8DStrategy()
    saved = v8d.calculate_rsi
    if rsi_fn is not None:
        v8d.calculate_rsi = rsi_fn
    try:
        evaluated = signals = 0
        side_pass: Dict[str, Counter] = {"ce": Counter(), "pe": Counter()}
        binding: Counter = Counter()
        day_signals: Dict[str, int] = defaultdict(int)
        days = set()
        decisions: List[Optional[str]] = []
        for i in range(60, len(candles)):
            t = candles[i]["timestamp"][11:16]
            hh, mm = int(t[:2]), int(t[3:5])
            if not (ENTRY_START <= dtime(hh, mm) <= ENTRY_END):
                decisions.append(None)
                continue
            window = candles[max(0, i + 1 - WINDOW): i + 1]
            e = explain_pullback(window, strat)
            days.add(candles[i]["timestamp"][:10])
            evaluated += 1
            for side in ("ce", "pe"):
                for k in ("trend", "pullback", "rsi", "reversal"):
                    if e[side][k]["pass"]:
                        side_pass[side][k] += 1
            if e["decision"]:
                signals += 1
                day_signals[candles[i]["timestamp"][:10]] += 1
            else:
                binding[e["binding_condition"]] += 1
            decisions.append(e["decision"])
        return {"evaluated_bars": evaluated, "signals": signals, "trading_days": len(days),
                "days_with_signal": len(day_signals), "side_pass": {k: dict(v) for k, v in side_pass.items()},
                "binding": dict(binding), "decisions": decisions}
    finally:
        v8d.calculate_rsi = saved


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    a = ap.parse_args()
    report: Dict[str, Any] = {}
    print(f"{'symbol':11s} {'bars':>6s} | {'signals (corrected RSI)':>24s} | {'signals (original lagged RSI)':>30s} | "
          f"{'days w/ signal':>14s} | RSI-zone decision flips")
    for sym in SYMBOL_FILES:
        c = load(sym)
        new = audit_symbol(c)
        old = audit_symbol(c, rsi_fn=lagged_rsi)
        flips = sum(1 for x, y in zip(new["decisions"], old["decisions"]) if x != y)
        report[sym] = {"corrected": {k: v for k, v in new.items() if k != "decisions"},
                       "original_lagged": {k: v for k, v in old.items() if k != "decisions"},
                       "decision_flips": flips}
        print(f"{sym:11s} {new['evaluated_bars']:6d} | {new['signals']:24d} | {old['signals']:30d} | "
              f"{new['days_with_signal']:>6d}/{new['trading_days']:<7d} | {flips}")
    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
