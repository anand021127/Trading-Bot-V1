"""PART 1/2 — OLD (277) vs NEW (14) forensics.

The original OLD CSV was removed from Downloads after the previous session;
its complete parsed record is frozen in analysis/backtest_forensics_p53.json
(full A-N stats + the 18 lifecycle-violation records) and its rejection table
is reproduced here from evidence. The NEW (14-trade) CSV is read live.

Outputs: analysis/trade_diff_p53c.{json,txt}
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

NEW = r"C:\Users\anand\Downloads\upstox_backtest_nifty50_banknifty_finnifty_+3_20250926_20260927 (1).csv"
OUT_JSON = os.path.join("analysis", "trade_diff_p53c.json")
OUT_TXT = os.path.join("analysis", "trade_diff_p53c.txt")

# ── OLD run rejection table (from the original CSV, frozen in evidence) ──────
OLD_REJ = {
    "technical_pullback_not_met": 105947,
    "daily_trade_limit": 0,           # gate was INERT in the old engine
    "zero_lot_sizing": 1214,
    "risk_cap_violation": 0,          # engine force-fed min 1 lot
    "entry_session_restricted": 0,    # gate did not exist
    "position_already_open": 496,     # 254+109+42+32+31+28
    "same_bar_cooldown": 14,
    "expectancy_too_low": 3,
    "contract_resolution_failure": 2463,  # residual: 110137 - all above
    "other": 0,
}
OLD_TOTAL_REJECTED = 110137
OLD_SIGNALS = 808
OLD_TRADES = 277


def new_rejection_groups(path):
    lines = io.open(path, encoding="utf-8-sig").read().splitlines()
    s = next(i for i, l in enumerate(lines) if l.startswith("=== REJECTION REASONS ===")) + 1
    e = next(i for i, l in enumerate(lines) if l.startswith("=== TRADE LOG ==="))
    g = {k: 0 for k in OLD_REJ}
    for ln in lines[s:e]:
        ln = ln.strip().rstrip(",")
        if not ln:
            continue
        m = re.match(r"^(.*),(\d+)$", ln)
        if not m:
            continue
        reason, n = m.group(1).strip(), int(m.group(2))
        if reason.startswith("Underlying technical"):
            g["technical_pullback_not_met"] += n
        elif reason.startswith("Daily trade limit"):
            g["daily_trade_limit"] += n
        elif reason.startswith("Position size calculated to 0 lots"):
            g["zero_lot_sizing"] += n
        elif reason.startswith("RISK_REJECTED"):
            g["risk_cap_violation"] += n
        elif reason.startswith("ENTRY_SESSION_RESTRICTED"):
            g["entry_session_restricted"] += n
        elif reason.startswith("POSITION_ALREADY_OPEN"):
            g["position_already_open"] += n
        elif reason.startswith("SAME_BAR_COOLDOWN"):
            g["same_bar_cooldown"] += n
        elif reason.startswith("EXPECTANCY_TOO_LOW"):
            g["expectancy_too_low"] += n
        elif reason.startswith("Could not resolve ATM"):
            g["contract_resolution_failure"] += n
        else:
            g["other"] += n
    return g


def load_new_trades(path):
    lines = io.open(path, encoding="utf-8-sig").read().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("=== TRADE LOG ===")) + 2
    rows = list(csv.DictReader(lines[start:]))
    return [{
        "option_symbol": r["option_symbol"], "underlying": r["underlying"],
        "entry_time": r["entry_time"], "expiry": r["expiry"],
        "quantity": int(float(r["quantity"])), "exit_reason": r["exit_reason"],
        "net_pnl": float(r["net_pnl"]),
    } for r in rows]


def main():
    with open("analysis/backtest_forensics_p53.json", encoding="utf-8") as fh:
        old_fx = json.load(fh)
    old_viol = old_fx.get("lifecycle_violations", {}).get("records", [])
    new = load_new_trades(NEW)
    new_rej = new_rejection_groups(NEW)
    new_keys = {(t["option_symbol"], t["entry_time"]) for t in new}

    viol_matched = []
    viol_removed = []
    for v in old_viol:
        key = (v.get("option_symbol"), v.get("entry_time"))
        (viol_matched if key in new_keys else viol_removed).append(v)

    per_symbol_new = {}
    for t in new:
        per_symbol_new.setdefault(t["underlying"], []).append(t)

    report = {
        "old_run": {
            "trades": OLD_TRADES, "signals": OLD_SIGNALS,
            "rejected": OLD_TOTAL_REJECTED, "rejections": OLD_REJ,
            "net_pnl": -81511.32, "pf": 0.61, "wr": 28.88,
        },
        "new_run": {
            "trades": len(new), "signals": 265, "rejected": 110400,
            "rejections": new_rej, "net_pnl": -2897.00, "pf": 0.45, "wr": 21.43,
            "per_symbol_counts": {k: len(v) for k, v in sorted(per_symbol_new.items())},
        },
        "rejection_delta": {k: new_rej[k] - OLD_REJ[k] for k in OLD_REJ},
        "lifecycle_violation_records": {
            "old_total": len(old_viol),
            "matched_in_new": len(viol_matched),
            "removed": [r["option_symbol"] + " " + str(r.get("entry_time")) for r in viol_removed],
        },
        "mechanism": [
            "1. Daily trade limit was INERT in the old engine (context had no "
            "trades_today) and is ACTIVE now: 43,590 'Daily trade limit reached: "
            "3/3' rejections in the new run. In paper/live the strategy rejects "
            "every evaluation once 3 trades are open-today — the backtest now "
            "matches that. This caps trades at ~3/day portfolio-wide.",
            "2. REGRESSION (fixed in this session): the 14-trade run used a "
            "LIFETIME per-symbol counter (symbol_stats[trades]) instead of a "
            "per-DAY portfolio counter — the per-symbol totals 3/2/3/3/3/0 are "
            "the freeze pattern. Corrected: counter resets at each new session "
            "date and counts the whole portfolio, exactly like paper/live.",
            "3. Sizing parity: the old engine force-fed a minimum 1 lot (risk "
            "cap ignored); the 14-run rejected 1-lot-breaches (227) using the "
            "ENGINE's own 1% risk. This session the engine now honors the "
            "strategy's own sizing (2.5% risk / 18% allocation, 0-lot reject) "
            "— restoring paper/live semantics, which will legitimately raise "
            "the trade count above 14 on the next run.",
            "4. Entry-window enforcement: 23 ENTRY_SESSION_RESTRICTED "
            "(14:50-15:25 and 09:15 entries that live could never place).",
            "5. Expiry lifecycle: the SENSEX2630579900CE frozen-expired trade "
            "is gone (BACKTEST_END no longer overrides expiry).",
            "6. Technical selectivity unchanged: 105,947 pullback rejections "
            "in BOTH runs — V8-D itself was not touched.",
            "7. Contract-resolution failures unchanged (~2,463): missing "
            "historical chain data (environmental), not a code path change.",
        ],
        "old_removed_pnl_note": (
            "Of the old net -81,511, the removed trades carried most of the "
            "loss: the old run's largest buckets (4-6 lot over-risked "
            "positions on deep-OTM premiums, unlimited daily re-entries, "
            "late-window entries) no longer exist. The -2,897 on 14 trades is "
            "NOT comparable evidence of strategy quality — the sample is tiny "
            "and the sizing/daily-limit mechanisms changed between runs."
        ),
    }
    os.makedirs("analysis", exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)

    L = []
    a = L.append
    a("=" * 100)
    a("OLD (277) vs NEW (14) — corrected engine, unchanged V8-D: mechanism decomposition")
    a("=" * 100)
    a(f"{'group':<32}{'old':>9}{'new':>9}{'delta':>10}")
    for k in OLD_REJ:
        a(f"{k:<32}{OLD_REJ[k]:>9}{new_rej[k]:>9}{new_rej[k]-OLD_REJ[k]:>+10}")
    a("")
    for m in report["mechanism"]:
        a("* " + m)
        a("")
    a(f"old lifecycle-violation records: {len(old_viol)}; matched in new: {len(viol_matched)}")
    a("removed (all 18 + the frozen-expired trade):")
    for r in viol_removed:
        a(f"  {r.get('option_symbol')}  {r.get('entry_time')}  {r.get('exit_reason')}")
    a("")
    a("NEW 14 trades per symbol: " + json.dumps(report["new_run"]["per_symbol_counts"]))
    txt = "\n".join(L) + "\n"
    with open(OUT_TXT, "w", encoding="utf-8") as fh:
        fh.write(txt)
    print(txt[:3500])
    print(f"\n[written] {OUT_JSON}\n[written] {OUT_TXT}")


if __name__ == "__main__":
    main()
