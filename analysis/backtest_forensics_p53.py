"""Forensic A–N analytics over the REAL Upstox v3 backtest CSV.

Input (never modified, never fabricated):
  C:\\Users\\anand\\Downloads\\upstox_backtest_nifty50_banknifty_finnifty_+3_20250926_20260927.csv
Real Upstox v3 5-minute data backtest, V8_D_PULLBACK_ATM, 2025-09-26 → 2026-09-27,
6 symbols, 277 trades / 808 signals / 110,774 candles scanned.

Outputs:
  analysis/backtest_forensics_p53.json  (machine-readable, all A–N sections)
  analysis/backtest_forensics_p53.txt   (human-readable report)

Sections:
  A  Overall (win rate, expectancy, PF, gross/net, maxDD, avg/median R,
     consecutive wins/losses)
  B  By symbol
  C  By CE vs PE
  D  By exit reason
  E  By month
  F  By time-of-day bucket
  G  By day of week
  H  By holding duration
  I  By DTE (days to expiry at entry)
  J  By entry premium bucket
  K  By stop distance (SL % of premium)
  L  By target distance (TP % of premium)
  M  Strategy/indicator fields present in the trade log (setup_score etc.)
  N  Regime clustering (proxy: consecutive-loss streaks vs calendar position,
     daily loss clustering, loss-streak depth distribution)

Also emits the lifecycle forensics for SENSEX2630579900CE and every
entry whose time violates the session window (after 14:45) or whose
exit_time > contract expiry.

BACKTEST ONLY — never wired into live/paper execution paths.
"""
from __future__ import annotations

import csv
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CSV_PATH = os.environ.get(
    "FORENSIC_CSV",
    r"C:\Users\anand\Downloads\upstox_backtest_nifty50_banknifty_finnifty_+3_20250926_20260927.csv",
)
OUT_JSON = os.path.join("analysis", "backtest_forensics_p53.json")
OUT_TXT = os.path.join("analysis", "backtest_forensics_p53.txt")

TRADE_COLS = [
    "timestamp", "underlying", "instrument_key", "option_symbol", "strike",
    "option_type", "expiry", "entry_time", "entry_price", "exit_time",
    "exit_price", "quantity", "lot_size", "stop_loss", "target",
    "trailing_stop", "exit_reason", "gross_pnl", "fees", "slippage",
    "net_pnl", "r_multiple", "setup_score", "strategy",
]


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def load_trades(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        lines = fh.read().splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("=== TRADE LOG ==="):
            start = i + 2  # skip header blank line
            break
    if start is None:
        raise SystemExit("TRADE LOG section not found")
    rows = []
    reader = csv.reader(lines[start:])
    header = next(reader)
    header = [h.strip() for h in header]
    for r in reader:
        if not r or not r[0].strip():
            continue
        rows.append({header[i]: r[i] for i in range(min(len(header), len(r)))})
    for row in rows:
        row["entry_dt"] = parse_ts(row["entry_time"])
        row["exit_dt"] = parse_ts(row["exit_time"])
        for k in ("entry_price", "exit_price", "stop_loss", "target",
                  "trailing_stop", "gross_pnl", "net_pnl", "fees",
                  "slippage", "r_multiple", "setup_score", "strike"):
            try:
                row[k] = float(row[k])
            except (ValueError, TypeError):
                row[k] = 0.0
        row["quantity"] = int(float(row["quantity"]))
        row["lot_size"] = int(float(row["lot_size"]))
        row["expiry_date"] = row["expiry"].strip()
        row["dte_days"] = (
            parse_ts(row["expiry"] + "T15:25:00+05:30").date() - row["entry_dt"].date()
        ).days
        row["duration_min"] = (row["exit_dt"] - row["entry_dt"]).total_seconds() / 60.0
        row["sl_pct"] = (
            (row["entry_price"] - row["stop_loss"]) / row["entry_price"] * 100.0
            if row["entry_price"] > 0 else 0.0
        )
        row["tp_pct"] = (
            (row["target"] - row["entry_price"]) / row["entry_price"] * 100.0
            if row["entry_price"] > 0 else 0.0
        )
    return rows


def section_stats(trades: list[dict], label: str) -> dict:
    n = len(trades)
    if n == 0:
        return {"label": label, "trades": 0}
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]
    gp = sum(t["net_pnl"] for t in wins)
    gl = abs(sum(t["net_pnl"] for t in losses))
    rs = [t["r_multiple"] for t in trades if isinstance(t["r_multiple"], (int, float))]
    # consecutive streaks (log is chronological by entry)
    ordered = sorted(trades, key=lambda t: t["entry_time"])
    max_w = max_l = cur_w = cur_l = 0
    worst_streak_pnl = 0.0
    run_pnl = 0.0
    for t in ordered:
        if t["net_pnl"] > 0:
            cur_w += 1
            cur_l = 0
            run_pnl = 0.0
        else:
            cur_l += 1
            cur_w = 0
            run_pnl += t["net_pnl"]
            if cur_l > max_l:
                max_l = cur_l
                worst_streak_pnl = run_pnl
        max_w = max(max_w, cur_w)
    return {
        "label": label,
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / n * 100, 2),
        "gross_profit": round(gp, 2),
        "gross_loss": round(-gl, 2) if gl else 0.0,
        "net_pnl": round(gp - gl, 2),
        "profit_factor": round(gp / gl, 3) if gl > 0 else None,
        "expectancy": round((gp - gl) / n, 2),
        "avg_win": round(gp / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gl / len(losses), 2) if losses else 0.0,
        "avg_r": round(statistics.fmean(rs), 3) if rs else None,
        "median_r": round(statistics.median(rs), 3) if rs else None,
        "max_consecutive_wins": max_w,
        "max_consecutive_losses": max_l,
        "worst_loss_streak_pnl": round(worst_streak_pnl, 2),
    }


def max_drawdown_pct(trades: list[dict]) -> float:
    ordered = sorted(trades, key=lambda t: t["exit_time"])
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for t in ordered:
        cum += t["net_pnl"]
        peak = max(peak, cum)
        if peak > 0:
            mdd = max(mdd, (peak - cum) / peak * 100.0)
    return round(mdd, 2)


def bucket_stats(trades: list[dict], keyfn, section: str) -> dict:
    groups: dict[str, list] = defaultdict(list)
    for t in trades:
        groups[str(keyfn(t))].append(t)
    out = {section: [section_stats(v, k) for k, v in sorted(groups.items())]}
    return out


def main() -> None:
    os.makedirs("analysis", exist_ok=True)
    with open(CSV_PATH, "r", encoding="utf-8-sig", newline="") as fh:
        text = fh.read()
    lines = text.splitlines()

    # Summary section (verbatim key/values for cross-check)
    summary: dict[str, str] = {}
    for ln in lines[1:32]:
        if "," in ln and ln.strip():
            k, _, v = ln.partition(",")
            summary[k.strip()] = v.strip()

    trades = load_trades(CSV_PATH)

    report: dict = {"source_file": CSV_PATH, "trade_rows_parsed": len(trades)}

    # ── Lifecycle forensics (the critical defect) ────────────────────────
    lifecycle = []
    session_violations = []
    for t in trades:
        exp_d = t["expiry_date"]
        exit_date = t["exit_dt"].date().isoformat()
        entry_t = t["entry_dt"].time()
        issues = []
        if exp_d and exit_date > exp_d:
            issues.append(f"exit {exit_date} AFTER contract expiry {exp_d}")
        if entry_t.hour > 14 or (entry_t.hour == 14 and entry_t.minute > 45):
            issues.append(f"entry {entry_t} after 14:45 last-entry cutoff")
        if entry_t.hour >= 15:
            issues.append(f"entry {entry_t} at/after 15:00 — past square-off zone")
        if issues:
            rec = {
                "option_symbol": t["option_symbol"],
                "underlying": t["underlying"],
                "expiry": exp_d,
                "entry_time": t["entry_time"],
                "exit_time": t["exit_time"],
                "exit_reason": t["exit_reason"],
                "entry_price": t["entry_price"],
                "exit_price": t["exit_price"],
                "gross_pnl": t["gross_pnl"],
                "net_pnl": t["net_pnl"],
                "issues": issues,
            }
            lifecycle.append(rec)
            if any("after 14:45" in i or "15:00" in i for i in issues):
                session_violations.append(rec)
    report["lifecycle_violations"] = {
        "count": len(lifecycle),
        "session_window_violations": len(session_violations),
        "records": lifecycle,
        "headline": next(
            (r for r in lifecycle if r["option_symbol"] == "SENSEX2630579900CE"), None
        ),
    }

    # ── A. Overall ────────────────────────────────────────────────────────
    overall = section_stats(trades, "ALL")
    overall["max_drawdown_pct_exit_ordered"] = max_drawdown_pct(trades)
    overall["csv_summary_cross_check"] = {
        "csv_total_trades": summary.get("Total Trades"),
        "csv_win_rate": summary.get("Win Rate %"),
        "csv_net_pnl": summary.get("Net P&L"),
        "csv_pf": summary.get("Profit Factor"),
        "csv_expectancy": summary.get("Expectancy"),
    }
    report["A_overall"] = overall

    # ── B–L grouped breakdowns ───────────────────────────────────────────
    report["B_by_symbol"] = bucket_stats(trades, lambda t: t["underlying"], "by_symbol")
    report["C_by_option_type"] = bucket_stats(trades, lambda t: t["option_type"], "by_option_type")
    report["D_by_exit_reason"] = bucket_stats(trades, lambda t: t["exit_reason"], "by_exit_reason")
    report["E_by_month"] = bucket_stats(trades, lambda t: t["entry_dt"].strftime("%Y-%m"), "by_month")
    report["F_by_time_of_day"] = bucket_stats(
        trades,
        lambda t: (
            "09:20-10:00" if t["entry_dt"].time() < __import__("datetime").time(10, 0)
            else "10:00-11:30" if t["entry_dt"].time() < __import__("datetime").time(11, 30)
            else "11:30-13:00" if t["entry_dt"].time() < __import__("datetime").time(13, 0)
            else "13:00-14:00" if t["entry_dt"].time() < __import__("datetime").time(14, 0)
            else "14:00-14:45" if t["entry_dt"].time() < __import__("datetime").time(14, 45)
            else "AFTER_14:45"
        ),
        "by_time_of_day",
    )
    report["G_by_day_of_week"] = bucket_stats(
        trades, lambda t: t["entry_dt"].strftime("%a"), "by_day_of_week"
    )

    def dur_bucket(t):
        m = t["duration_min"]
        if m <= 5:
            return "0-5m"
        if m <= 15:
            return "5-15m"
        if m <= 30:
            return "15-30m"
        if m <= 60:
            return "30-60m"
        if m <= 120:
            return "1-2h"
        if m <= 240:
            return "2-4h"
        if m <= 1440:
            return "4h-1d"
        return ">1d"

    report["H_by_holding_duration"] = bucket_stats(trades, dur_bucket, "by_duration")
    report["I_by_dte"] = bucket_stats(trades, lambda t: f"DTE_{t['dte_days']}", "by_dte")

    def prem_bucket(t):
        p = t["entry_price"]
        if p < 50:
            return "<50"
        if p < 100:
            return "50-100"
        if p < 150:
            return "100-150"
        if p < 250:
            return "150-250"
        if p < 400:
            return "250-400"
        return ">=400"

    report["J_by_entry_premium"] = bucket_stats(trades, prem_bucket, "by_premium")

    def sl_bucket(t):
        s = t["sl_pct"]
        if s < 10:
            return "<10%"
        if s < 15:
            return "10-15%"
        if s < 20:
            return "15-20%"
        if s < 25:
            return "20-25%"
        if s < 30:
            return "25-30%"
        return ">=30%"

    def tp_bucket(t):
        s = t["tp_pct"]
        if s < 20:
            return "<20%"
        if s < 30:
            return "20-30%"
        if s < 40:
            return "30-40%"
        if s < 50:
            return "40-50%"
        if s < 60:
            return "50-60%"
        return ">=60%"

    report["K_by_stop_distance"] = bucket_stats(trades, sl_bucket, "by_stop_distance")
    report["L_by_target_distance"] = bucket_stats(trades, tp_bucket, "by_target_distance")

    # ── M. Available strategy/indicator fields ───────────────────────────
    setup_scores = [t["setup_score"] for t in trades]
    report["M_indicator_fields"] = {
        "fields_in_trade_log": TRADE_COLS,
        "setup_score": {
            "unique_values": sorted(set(setup_scores)),
            "mean": round(statistics.fmean(setup_scores), 3) if setup_scores else None,
            "note": "setup_score is constant 100 for every accepted trade — it is a "
                    "transparency score (all conditions passed), NOT an edge signal. "
                    "Deeper indicator fields (EMA20/50, RSI, ATR, pullback distance) "
                    "are NOT in this CSV; backtest-only signal_diagnostics capture was "
                    "added to the engine this session for future runs.",
        },
        "r_multiple_stats": {
            "mean": round(statistics.fmean([t["r_multiple"] for t in trades]), 3),
            "median": round(statistics.median([t["r_multiple"] for t in trades]), 3),
            "min": min(t["r_multiple"] for t in trades),
            "max": max(t["r_multiple"] for t in trades),
        },
    }

    # ── N. Regime clustering (honest proxies from trade data) ────────────
    ordered = sorted(trades, key=lambda t: t["entry_time"])
    # daily aggregation: is P&L concentrated in a few bad days?
    day_pnl: dict[str, float] = defaultdict(float)
    day_trades: dict[str, int] = defaultdict(int)
    for t in ordered:
        day_pnl[t["exit_dt"].date().isoformat()] += t["net_pnl"]
        day_trades[t["exit_dt"].date().isoformat()] += 1
    worst_days = sorted(day_pnl.items(), key=lambda kv: kv[1])[:10]
    total_net = overall["net_pnl"]
    worst5_sum = sum(v for _, v in sorted(day_pnl.items(), key=lambda kv: kv[1])[:5])
    # loss-streak depth distribution
    streaks = []
    cur = []
    for t in ordered:
        if t["net_pnl"] <= 0:
            cur.append(t["net_pnl"])
        else:
            if cur:
                streaks.append({"length": len(cur), "pnl": round(sum(cur), 2)})
                cur = []
    if cur:
        streaks.append({"length": len(cur), "pnl": round(sum(cur), 2)})
    streaks.sort(key=lambda s: s["pnl"])
    report["N_regime_clustering"] = {
        "active_trading_days": len(day_pnl),
        "days_with_losses": sum(1 for v in day_pnl.values() if v < 0),
        "worst_5_days_sum": round(worst5_sum, 2),
        "worst_5_days_share_of_net_pct": (
            round(worst5_sum / total_net * 100, 1) if total_net < 0 else None
        ),
        "worst_10_days": [{"day": d, "net_pnl": round(v, 2), "trades": day_trades[d]} for d, v in worst_days],
        "loss_streaks_top10": streaks[:10],
        "monthly_net_check": [
            {"month": s["label"], "net_pnl": s["net_pnl"], "trades": s["trades"], "win_rate_pct": s["win_rate_pct"]}
            for s in report["E_by_month"]["by_month"]
        ],
        "interpretation": "Losses are persistent across months and symbols (no single "
                          "regime dominates), consistent with a strategy whose edge is "
                          "negative after costs rather than one good/bad market phase.",
    }

    # ── Cost share ───────────────────────────────────────────────────────
    total_fees = sum(t["fees"] for t in trades)
    total_slip = sum(t["slippage"] for t in trades)
    report["costs"] = {
        "total_fees": round(total_fees, 2),
        "total_slippage": round(total_slip, 2),
        "gross_abs_pnl": round(sum(abs(t["gross_pnl"]) for t in trades), 2),
        "fees_vs_gross_turnover_pct": round(
            total_fees / sum(abs(t["gross_pnl"]) for t in trades) * 100, 2
        ) if trades else None,
    }

    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False, default=str)

    # Human-readable report
    def fmt_row(s: dict) -> str:
        if s.get("trades", 0) == 0:
            return f"  {s['label']:<18} no trades"
        return (
            f"  {s['label']:<18} n={s['trades']:<4} wr={s['win_rate_pct']:<6}% "
            f"net={s['net_pnl']:>10} PF={s['profit_factor']} exp={s['expectancy']:>8} "
            f"avgR={s['avg_r']}"
        )

    lines_out = []
    ap = lines_out.append
    ap("=" * 110)
    ap("V8-D FORENSIC ANALYTICS — REAL UPSTOX v3 BACKTEST (2025-09-26 → 2026-09-27, 6 symbols)")
    ap("=" * 110)
    ap(f"Trades parsed: {len(trades)}  | CSV cross-check: {summary.get('Total Trades')} trades, "
       f"WR {summary.get('Win Rate %')}%, net {summary.get('Net P&L')}, PF {summary.get('Profit Factor')}")
    ap("")
    lv = report["lifecycle_violations"]
    ap(f"LIFECYCLE VIOLATIONS: {lv['count']} trades with expiry/session issues "
       f"({lv['session_window_violations']} entry-session violations)")
    if lv["headline"]:
        h = lv["headline"]
        ap(f"  HEADLINE: {h['option_symbol']} entry {h['entry_time']} expiry {h['expiry']} "
           f"exit {h['exit_time']} ({h['exit_reason']}) gross={h['gross_pnl']} — issues: {h['issues']}")
    for r in lifecycle[:12]:
        ap(f"  - {r['option_symbol']}: {', '.join(r['issues'])} | exit_reason={r['exit_reason']}")
    ap("")
    ap("A. OVERALL:")
    for k, v in overall.items():
        if k not in ("label", "csv_summary_cross_check"):
            ap(f"  {k:<32} {v}")
    ap("")
    for sec, title in [
        ("B_by_symbol", "B. BY SYMBOL"),
        ("C_by_option_type", "C. BY CE/PE"),
        ("D_by_exit_reason", "D. BY EXIT REASON"),
        ("E_by_month", "E. BY MONTH"),
        ("F_by_time_of_day", "F. BY TIME OF DAY"),
        ("G_by_day_of_week", "G. BY DAY OF WEEK"),
        ("H_by_holding_duration", "H. BY HOLDING DURATION"),
        ("I_by_dte", "I. BY DTE"),
        ("J_by_entry_premium", "J. BY ENTRY PREMIUM"),
        ("K_by_stop_distance", "K. BY STOP DISTANCE"),
        ("L_by_target_distance", "L. BY TARGET DISTANCE"),
    ]:
        ap(title)
        key = list(report[sec].keys())[0]
        for s in report[sec][key]:
            ap(fmt_row(s))
        ap("")
    ap("M. INDICATOR FIELDS:")
    ap(f"  setup_score unique values: {report['M_indicator_fields']['setup_score']['unique_values']}")
    ap(f"  {report['M_indicator_fields']['setup_score']['note']}")
    ap("")
    ap("N. REGIME CLUSTERING:")
    for k, v in report["N_regime_clustering"].items():
        if k not in ("worst_10_days", "loss_streaks_top10", "monthly_net_check"):
            ap(f"  {k}: {v}")
    ap("  worst 10 days:")
    for d in report["N_regime_clustering"]["worst_10_days"]:
        ap(f"    {d['day']}  net={d['net_pnl']:>10}  trades={d['trades']}")
    ap("  deepest loss streaks:")
    for s in report["N_regime_clustering"]["loss_streaks_top10"][:6]:
        ap(f"    length={s['length']}  pnl={s['pnl']}")
    ap("")
    ap("COSTS:")
    for k, v in report["costs"].items():
        ap(f"  {k}: {v}")

    with open(OUT_TXT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines_out) + "\n")

    print("\n".join(lines_out[:40]))
    print(f"\n[written] {OUT_JSON}\n[written] {OUT_TXT}")


if __name__ == "__main__":
    main()
