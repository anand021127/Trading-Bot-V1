#!/usr/bin/env python3
"""Pre-registered intraday strategy research on REAL 5-minute index candles (real_data/).

WHY THIS EXISTS
  A strategy may only replace V8-D if it shows a statistically defensible edge, and "it traded
  more" is not evidence. This harness evaluates candidate entry rules with the SAME exit bracket,
  fixed rules (no parameter tuning), causal signals (decided on a COMPLETED bar, entered at the
  NEXT bar's open), costs charged, day-clustered bootstrap statistics, a time split, a per-symbol
  split and an untouched hold-out period.

WHAT IT CAN AND CANNOT SAY
  It measures edge on the real UNDERLYING. The repo carries no historical option prices and
  inventing them is forbidden, so option P&L (theta, gamma, spread) is NOT simulated. Underlying
  edge is necessary, not sufficient: a candidate that shows no underlying edge cannot be profitable
  as an options strategy; one that does must still be forward-tested on real option data.

DECISION RULE (fixed before looking at results)
  A candidate qualifies only if ALL hold, using Bonferroni-adjusted confidence for the number of
  candidates tested:
    1. mean net R > 0 with the day-clustered bootstrap lower bound > 0 (pooled, all symbols);
    2. mean net R > 0 in BOTH time halves (H1 Jan-Jun, H2 Jul-Dec 2024);
    3. mean net R > 0 on at least 4 of the 6 symbols;
    4. mean net R > 0 on the untouched hold-out (NIFTY50 Jan-Mar 2025).
  The random-entry CONTROL (20 seeds) must come out ~0 gross (validates the simulator).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.indicators.atr import calculate_atr  # noqa: E402
from backend.indicators.ema import calculate_ema  # noqa: E402
from backend.indicators.rsi import calculate_rsi  # noqa: E402

SYMBOLS = ["NIFTY50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"]
STOP_ATR, RR = 1.5, 1.5            # identical bracket for every candidate: stop 1.5 ATR, target 1.5R
COST_BP = 1.5                      # round-trip cost, basis points of spot (spread+slippage+fees on ATM options, delta~0.5)
EXIT_MIN = 15 * 60 + 10            # square-off: first bar starting at/after 15:10 IST
ENTRY_FROM, ENTRY_TO = 9 * 60 + 30, 14 * 60 + 30


class Series:
    def __init__(self, candles: List[Dict[str, Any]]):
        self.n = len(candles)
        self.o = np.array([c["open"] for c in candles], float)
        self.h = np.array([c["high"] for c in candles], float)
        self.l = np.array([c["low"] for c in candles], float)
        self.c = np.array([c["close"] for c in candles], float)
        self.date = [c["timestamp"][:10] for c in candles]
        self.min = np.array([int(c["timestamp"][11:13]) * 60 + int(c["timestamp"][14:16]) for c in candles])
        self.candles = candles
        pad = lambda a: np.concatenate([np.full(self.n - len(a), np.nan), np.array(a, float)])
        cl = list(self.c)
        self.ema20, self.ema50 = pad(calculate_ema(cl, 20)), pad(calculate_ema(cl, 50))
        self.rsi = pad(calculate_rsi(cl, 14))
        self.atr = pad(calculate_atr(list(self.h), list(self.l), cl, 14))
        self.day_first: Dict[str, int] = {}
        self.day_last: Dict[str, int] = {}
        for i, d in enumerate(self.date):
            self.day_first.setdefault(d, i)
            self.day_last[d] = i


def simulate(s: Series, i: int, direction: int) -> Optional[Tuple[float, int]]:
    """Signal decided on COMPLETED bar i; enter at open of bar i+1. Returns (R_gross, exit_index)."""
    j0 = i + 1
    if j0 >= s.n or s.date[j0] != s.date[i] or np.isnan(s.atr[i]) or s.atr[i] <= 0:
        return None
    entry = s.o[j0]
    risk = STOP_ATR * s.atr[i]
    stop = entry - direction * risk
    target = entry + direction * RR * risk
    j = j0
    while j <= s.day_last[s.date[i]]:
        if s.min[j] >= EXIT_MIN and j > j0:
            return direction * (s.o[j] - entry) / risk, j
        lo, hi, op = s.l[j], s.h[j], s.o[j]
        # Gaps are filled at the OPEN on BOTH sides (symmetric): a gap through the stop is worse
        # than -1R, a gap through the target is better than +RR.
        if direction > 0:
            if op <= stop or op >= target:
                return (op - entry) / risk, j
            hit_stop, hit_tgt = lo <= stop, hi >= target
        else:
            if op >= stop or op <= target:
                return (entry - op) / risk, j
            hit_stop, hit_tgt = hi >= stop, lo <= target
        # A 5-minute bar touching BOTH levels does not reveal which came first: value it at its
        # expectation (50/50) — neutral, not pessimistic and not optimistic.
        if hit_stop and hit_tgt:
            return (RR - 1.0) / 2.0, j
        if hit_stop:
            return -1.0, j
        if hit_tgt:
            return RR, j
        j += 1
    return direction * (s.c[j - 1] - entry) / risk, j - 1


def run(s: Series, signals: Dict[int, int]) -> List[Dict[str, Any]]:
    """One position at a time per symbol; later signals while a trade is open are skipped."""
    trades, free_at = [], -1
    for i in sorted(signals):
        if i < free_at or not (ENTRY_FROM <= s.min[i] + 5 <= ENTRY_TO + 5):    # filter on the ENTRY bar's time
            continue
        r = simulate(s, i, signals[i])
        if r is None:
            continue
        rg, jx = r
        risk_pts = STOP_ATR * s.atr[i]
        cost_r = (COST_BP / 1e4) * s.c[i] / risk_pts
        trades.append({"i": i, "date": s.date[i], "dir": signals[i], "R_gross": rg, "R_net": rg - cost_r})
        free_at = jx + 1
    return trades


# ───────────────────────── candidate signal generators (causal, fixed rules) ─────────────────────────
def sig_random(s: Series, seed: int = 11) -> Dict[int, int]:
    rng = np.random.default_rng(seed)
    out: Dict[int, int] = {}
    for d, a in s.day_first.items():
        cand = [i for i in range(a, s.day_last[d] + 1) if ENTRY_FROM <= s.min[i] <= ENTRY_TO]
        if cand:
            out[int(rng.choice(cand))] = int(rng.choice([-1, 1]))
    return out


def sig_v8d(s: Series) -> Dict[int, int]:
    """Current V8-D entry (after the RSI fix), via the SAME diagnostics module the bot uses."""
    from backend.strategy.v8d_diagnostics import explain_pullback
    out: Dict[int, int] = {}
    for i in range(60, s.n):
        if not (ENTRY_FROM <= s.min[i] + 5 <= ENTRY_TO + 5):
            continue
        e = explain_pullback(s.candles[max(0, i - 119): i + 1])
        if e["decision"]:
            out[i] = 1 if e["decision"] == "CE" else -1
    return out


def sig_v8d_original_lag(s: Series) -> Dict[int, int]:
    """V8-D with the ORIGINAL (one-bar-stale) RSI — what produced the user's real option backtest."""
    from backend.strategy.strategies import v8d_strategy as m
    keep = m.calculate_rsi
    m.calculate_rsi = lambda v, p=14: keep(v[:-1], p) if len(v) > p + 1 else []
    try:
        return sig_v8d(s)
    finally:
        m.calculate_rsi = keep


def sig_orb30(s: Series) -> Dict[int, int]:
    """Opening-range breakout: first 6 bars (09:15-09:44) range; first completed close beyond it, once a day."""
    out: Dict[int, int] = {}
    for d, a in s.day_first.items():
        if s.day_last[d] - a < 12:
            continue
        hi, lo = s.h[a:a + 6].max(), s.l[a:a + 6].min()
        for i in range(a + 6, s.day_last[d] + 1):
            if s.min[i] > 13 * 60:
                break
            if s.c[i] > hi:
                out[i] = 1
                break
            if s.c[i] < lo:
                out[i] = -1
                break
    return out


def sig_trend_cross(s: Series) -> Dict[int, int]:
    """Trend continuation: EMA20/50 trend (>0.15% apart) and the close re-crosses EMA20 in the trend direction."""
    out: Dict[int, int] = {}
    for i in range(51, s.n):
        e20, e50 = s.ema20[i], s.ema50[i]
        if np.isnan(e20) or np.isnan(e50):
            continue
        sep = (e20 - e50) / e50
        if sep > 0.0015 and s.c[i - 1] <= s.ema20[i - 1] and s.c[i] > e20:
            out[i] = 1
        elif sep < -0.0015 and s.c[i - 1] >= s.ema20[i - 1] and s.c[i] < e20:
            out[i] = -1
    return out


def sig_mean_rev(s: Series) -> Dict[int, int]:
    """Mean reversion: close outside 2-sigma Bollinger(20) with RSI(14) at an extreme -> fade it."""
    out: Dict[int, int] = {}
    for i in range(30, s.n):
        w = s.c[i - 19: i + 1]
        m, sd = w.mean(), w.std()
        if sd <= 0 or np.isnan(s.rsi[i]):
            continue
        if s.c[i] < m - 2 * sd and s.rsi[i] < 30:
            out[i] = 1
        elif s.c[i] > m + 2 * sd and s.rsi[i] > 70:
            out[i] = -1
    return out


def sig_morning_momentum(s: Series) -> Dict[int, int]:
    """Intraday momentum: morning return (open -> 12:55 close) beyond 0.3% continues into the afternoon; once a day."""
    out: Dict[int, int] = {}
    for d, a in s.day_first.items():
        idx = [i for i in range(a, s.day_last[d] + 1) if s.min[i] == 12 * 60 + 55]
        if not idx:
            continue
        i = idx[0]
        ret = s.c[i] / s.o[a] - 1
        if abs(ret) > 0.003:
            out[i] = 1 if ret > 0 else -1
    return out


def sig_gap_go(s: Series) -> Dict[int, int]:
    """Gap-and-go: opening gap > 0.25% that holds through the first 15 minutes continues; once a day."""
    out: Dict[int, int] = {}
    days = sorted(s.day_first)
    for k in range(1, len(days)):
        a, prev_last = s.day_first[days[k]], s.day_last[days[k - 1]]
        gap = s.o[a] / s.c[prev_last] - 1
        if abs(gap) > 0.0025 and s.day_last[days[k]] - a > 4:
            i = a + 2                                    # completed 09:15, 09:20, 09:25 bars
            if (gap > 0 and s.c[i] > s.o[a]) or (gap < 0 and s.c[i] < s.o[a]):
                out[i] = 1 if gap > 0 else -1
    return out


CANDIDATES: Dict[str, Callable[[Series], Dict[int, int]]] = {
    "V8D_original_lagRSI": sig_v8d_original_lag,
    "V8D_corrected": sig_v8d, "ORB30": sig_orb30, "TREND_CROSS": sig_trend_cross,
    "MEAN_REV": sig_mean_rev, "MORNING_MOMENTUM": sig_morning_momentum, "GAP_GO": sig_gap_go,
}


# ───────────────────────── statistics ─────────────────────────
def cluster_bootstrap(trades: List[Dict[str, Any]], key: str, n_boot: int = 4000, seed: int = 5) -> Dict[str, float]:
    """Resample whole (symbol, day) clusters — trades on the same day are not independent."""
    if not trades:
        return {"n": 0}
    groups: Dict[Tuple[str, str], List[float]] = {}
    for t in trades:
        groups.setdefault((t["symbol"], t["date"]), []).append(t[key])
    keys = list(groups)
    sums = np.array([sum(groups[k]) for k in keys]); cnt = np.array([len(groups[k]) for k in keys])
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    for b in range(n_boot):
        pick = rng.integers(0, len(keys), len(keys))
        means[b] = sums[pick].sum() / max(cnt[pick].sum(), 1)
    x = np.array([t[key] for t in trades])
    return {"n": len(x), "mean": float(x.mean()), "hit": float((x > 0).mean()),
            "lo95": float(np.percentile(means, 2.5)), "hi95": float(np.percentile(means, 97.5)),
            "lo99_4": float(np.percentile(means, 0.3)), "p_pos": float((means > 0).mean())}


def load(sym: str, fname: Optional[str] = None) -> Series:
    return Series(json.loads((ROOT / "real_data" / (fname or f"{sym}_2024_5min.json")).read_text()))


def evaluate(name: str, gen: Callable[[Series], Dict[int, int]], series: Dict[str, Series]) -> List[Dict[str, Any]]:
    trades = []
    for sym, s in series.items():
        for t in run(s, gen(s)):
            t["symbol"] = sym
            trades.append(t)
    return trades


def main() -> int:
    series = {sym: load(sym) for sym in SYMBOLS}
    holdout = {"NIFTY50": load("NIFTY50", "NIFTY50_2025Q1_5min.json")}
    results: Dict[str, Any] = {}
    names = ["CONTROL_RANDOM"] + list(CANDIDATES)
    gens = {"CONTROL_RANDOM": sig_random, **CANDIDATES}
    for name in names:
        if name == "CONTROL_RANDOM":                 # 20 independent random-entry draws: measures simulator bias
            tr, ho, seed_means = [], [], []
            for seed in range(20):
                one = evaluate(name, lambda s, sd=seed: sig_random(s, sd), series)
                tr += one
                ho += evaluate(name, lambda s, sd=seed: sig_random(s, sd), holdout)
                seed_means.append(float(np.mean([t["R_gross"] for t in one])))
            results["_control_seed_gross_means"] = seed_means
        else:
            tr = evaluate(name, gens[name], series)
            ho = evaluate(name, gens[name], holdout)
        h1 = [t for t in tr if t["date"] < "2024-07-01"]
        h2 = [t for t in tr if t["date"] >= "2024-07-01"]
        per_sym = {sym: float(np.mean([t["R_net"] for t in tr if t["symbol"] == sym])) if any(t["symbol"] == sym for t in tr) else None
                   for sym in SYMBOLS}
        results[name] = {
            "net": cluster_bootstrap(tr, "R_net"), "gross": cluster_bootstrap(tr, "R_gross"),
            "H1": cluster_bootstrap(h1, "R_net"), "H2": cluster_bootstrap(h2, "R_net"),
            "holdout": cluster_bootstrap(ho, "R_net"), "per_symbol_net": per_sym,
        }
        n = results[name]["net"]
        pos = sum(1 for v in per_sym.values() if v is not None and v > 0)
        qual = (name != "CONTROL_RANDOM" and n.get("lo99_4", -1) > 0 and results[name]["H1"].get("mean", -1) > 0
                and results[name]["H2"].get("mean", -1) > 0 and pos >= 4 and results[name]["holdout"].get("mean", -1) > 0)
        results[name]["qualifies"] = bool(qual)
        results[name]["symbols_positive"] = pos
    sm = results.get("_control_seed_gross_means") or [0.0]
    bias = float(np.mean(sm))
    (ROOT / "analysis").mkdir(exist_ok=True)
    out = ROOT / "analysis" / "strategy_research_results.json"
    out.write_text(json.dumps({"stop_atr": STOP_ATR, "rr": RR, "cost_bp": COST_BP, "results": results}, indent=2))
    print(f"bracket: stop {STOP_ATR} ATR / target {RR}R, cost {COST_BP} bp round trip; completed-bar signals, next-open entry\n")
    print(f"random-entry control over 20 seeds: mean gross R {bias:+.3f} (seed range {min(sm):+.3f}..{max(sm):+.3f}) -> "
          f"simulator bias {'OK (|bias|<0.03)' if abs(bias) < 0.03 else 'CHECK'}")
    print(f"{'candidate':20s} {'n':>5s} {'hit%':>5s} {'grossR':>7s} {'netR':>7s} {'net 95% CI':>17s} {'H1':>7s} {'H2':>7s} {'sym+':>4s} {'holdout':>8s}  qualifies")
    for name in names:
        r = results[name]; n, g = r["net"], r["gross"]
        if n.get("n", 0) == 0:
            print(f"{name:20s}     0 trades"); continue
        print(f"{name:20s} {n['n']:5d} {100*n['hit']:5.1f} {g['mean']:+7.3f} {n['mean']:+7.3f} [{n['lo95']:+.3f},{n['hi95']:+.3f}] "
              f"{r['H1'].get('mean', float('nan')):+7.3f} {r['H2'].get('mean', float('nan')):+7.3f} {r['symbols_positive']:4d} "
              f"{r['holdout'].get('mean', float('nan')):+8.3f}  {'YES' if r['qualifies'] else 'no'}")
    ref = results["V8D_corrected"]["net"]
    half = (ref["hi95"] - ref["lo95"]) / 2
    print(f"\nPower: the pooled 95% CI half-width is about +/-{half:.3f} R, so with ~{ref['n']} trades per candidate only an edge of "
          f"~{2.8 * half / 1.96:.2f} R/trade or more is reliably detectable (80% power). A real edge smaller than that cannot be "
          "confirmed (or ruled out) with one year of data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
