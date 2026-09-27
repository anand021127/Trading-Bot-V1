"""Evidence-based V8-D evaluation: does ANY change to the frozen parameters
or filter thresholds improve expectancy on held-out data?

Method (per the strategy-modification rules — no single-period fitting):
  - Chronological split of the 277 real trades: TRAIN = first ~70% of the
    backtest window (2025-09-26 → 2026-06-30), VALIDATION = final ~30%
    (2026-07-01 → 2026-09-27). The strategy was NOT fitted on either half;
    this checks whether any observed pattern is stable in time.
  - Candidate dimensions (from the A–N forensics, all pre-declared):
      DTE_0 participation, stop-distance (25-30% vs >=30%), target distance,
      time-of-day, day-of-week. A change is considered ONLY if it improves
      BOTH train and validation expectancy/PF.
  - No change may be justified from a single split improvement alone.

Output: analysis/v8d_old_vs_new_p53.json (+ printed verdict).
BACKTEST ANALYSIS ONLY — never touches live/paper execution.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from analysis.backtest_forensics_p53 import CSV_PATH, load_trades, section_stats, max_drawdown_pct  # noqa: E402

OUT_JSON = os.path.join("analysis", "v8d_old_vs_new_p53.json")
SPLIT_DATE = "2026-07-01"  # ~70/30 chronological split of the 12-month window


def old_v8d(trades):
    """OLD: the exact frozen strategy — all trades the engine took."""
    return list(trades)


def no_0dte(trades):
    """NEW candidate 1: skip 0-DTE entries (forensics: DTE_0 wr 20.0%, exp
    -448.76 vs DTE>=1 wr 30.5%, exp -269)."""
    return [t for t in trades if t["dte_days"] >= 1]


def tighter_stop_only(trades):
    """NEW candidate 2: skip trades whose stop distance is >= 30% of premium
    (forensics: 25-30% bucket PF 0.715 vs >=30% PF 0.397)."""
    return [t for t in trades if t["sl_pct"] < 30.0]


def no_0dte_and_tighter_stop(trades):
    """NEW candidate 3: both filters combined."""
    return [t for t in trades if t["dte_days"] >= 1 and t["sl_pct"] < 30.0]


def morning_only(trades):
    """NEW candidate 4: entries only in 09:20-11:30 (forensics: 10:00-11:30
    bucket is the worst slot; 09:20-10:00 mildly negative, 11:30+ mixed)."""
    from datetime import time as dtime
    return [t for t in trades if dtime(9, 20) <= t["entry_dt"].time() < dtime(11, 30)]


CANDIDATES = {
    "OLD_frozen_v8d": old_v8d,
    "NEW_no_0dte": no_0dte,
    "NEW_stop_below_30pct": tighter_stop_only,
    "NEW_no_0dte_plus_stop_below_30pct": no_0dte_and_tighter_stop,
    "NEW_morning_only_0920_1130": morning_only,
}


def main() -> None:
    trades = load_trades(CSV_PATH)
    train = [t for t in trades if t["entry_dt"].date().isoformat() < SPLIT_DATE]
    valid = [t for t in trades if t["entry_dt"].date().isoformat() >= SPLIT_DATE]

    results = {
        "method": {
            "split_date": SPLIT_DATE,
            "train_trades": len(train),
            "train_window": f"2025-09-26 → {SPLIT_DATE}",
            "validation_trades": len(valid),
            "validation_window": f"{SPLIT_DATE} → 2026-09-27",
            "rule": "A candidate may only be considered if BOTH train and "
                    "validation expectancy and profit factor improve vs OLD. "
                    "A single-period improvement is explicitly insufficient.",
        },
        "candidates": {},
    }

    for name, fn in CANDIDATES.items():
        tr = fn(trades)
        tr_train = fn(train)
        tr_valid = fn(valid)
        results["candidates"][name] = {
            "full": section_stats(tr, name),
            "train": section_stats(tr_train, name),
            "validation": section_stats(tr_valid, name),
            "max_drawdown_pct_full": max_drawdown_pct(tr),
            "rejected_trade_count": len(trades) - len(tr),
        }

    old = results["candidates"]["OLD_frozen_v8d"]
    verdicts = {}
    for name, res in results["candidates"].items():
        if name == "OLD_frozen_v8d":
            continue
        train_better = (
            res["train"]["expectancy"] > old["train"]["expectancy"]
            and (res["train"]["profit_factor"] or 0) > (old["train"]["profit_factor"] or 0)
        )
        valid_better = (
            res["validation"]["expectancy"] > old["validation"]["expectancy"]
            and (res["validation"]["profit_factor"] or 0) > (old["validation"]["profit_factor"] or 0)
        )
        still_negative = (
            res["validation"]["expectancy"] is not None
            and res["validation"]["expectancy"] <= 0
        )
        verdicts[name] = {
            "train_improved": bool(train_better),
            "validation_improved": bool(valid_better),
            "validation_expectancy_still_negative": bool(still_negative),
            "adopt": bool(train_better and valid_better and not still_negative),
        }
    results["verdicts"] = verdicts
    results["decision"] = (
        "ADOPT none of the candidates. Every filter that looks better in-sample "
        "either fails the validation half or still leaves expectancy negative. "
        "Per the strategy-modification rule, V8-D stays FROZEN; the defective "
        "backtest engine (expiry lifecycle + entry window) was fixed instead, "
        "and backtest-only signal diagnostics were added so the NEXT real run "
        "can measure true signal edge (EMA/RSI/ATR at entry, forward movement)."
        if not any(v["adopt"] for v in verdicts.values())
        else "One or more candidates passed both halves — see verdicts."
    )

    os.makedirs("analysis", exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, ensure_ascii=False, default=str)

    print(f"Split: train n={len(train)} / validation n={len(valid)}")
    for name, res in results["candidates"].items():
        f, tr, va = res["full"], res["train"], res["validation"]
        print(f"\n{name}: full n={f['trades']} net={f['net_pnl']} PF={f['profit_factor']}")
        print(f"  train      n={tr['trades']:<4} wr={tr['win_rate_pct']}% exp={tr['expectancy']} PF={tr['profit_factor']}")
        print(f"  validation n={va['trades']:<4} wr={va['win_rate_pct']}% exp={va['expectancy']} PF={va['profit_factor']}")
    print("\nVERDICTS:")
    for name, v in verdicts.items():
        print(f"  {name}: {v}")
    print(f"\nDECISION: {results['decision']}")
    print(f"[written] {OUT_JSON}")


if __name__ == "__main__":
    main()
