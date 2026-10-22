"""Does a backtest show a statistically defensible edge? (fixed rules, no tuning)

P&L alone is a poor guide: a profitable-looking run can be noise, and a losing one can be bad luck.
This computes the mean result per trade in R-multiples (net P&L / money at risk), a bootstrap 95 %
confidence interval and a t-statistic, then a plain verdict:

    INSUFFICIENT_DATA      fewer than 30 trades — nothing can be concluded
    POSITIVE_EXPECTANCY    the whole 95 % interval is above zero
    NEGATIVE_EXPECTANCY    the whole 95 % interval is below zero (statistically significant loss)
    NO_DEMONSTRATED_EDGE   the interval includes zero (cannot tell from luck)

Caveats stated in the output: trades are treated as independent (they are not perfectly), and a
single backtest period is one sample of the market.
"""
from __future__ import annotations

import math
import random
from typing import Any, Dict, List, Optional

MIN_TRADES = 30
N_BOOT = 4000


def _r_multiples(trades: List[Dict[str, Any]], pnl_key: str) -> List[float]:
    out: List[float] = []
    for t in trades:
        try:
            risk = (float(t["entry_price"]) - float(t["stop_loss"])) * float(t["quantity"])
            if risk > 0:
                out.append(float(t[pnl_key]) / risk)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _stats(x: List[float], seed: int) -> Dict[str, Optional[float]]:
    n = len(x)
    mean = sum(x) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in x) / (n - 1)) if n > 1 else 0.0
    rng = random.Random(seed)
    boots = sorted(sum(x[rng.randrange(n)] for _ in range(n)) / n for _ in range(N_BOOT))
    return {"mean_r": round(mean, 4), "t_stat": round(mean / (sd / math.sqrt(n)), 2) if sd > 0 else None,
            "ci95_low": round(boots[int(0.025 * N_BOOT)], 4), "ci95_high": round(boots[int(0.975 * N_BOOT) - 1], 4)}


def edge_assessment(trade_log: List[Dict[str, Any]], seed: int = 7) -> Dict[str, Any]:
    net = _r_multiples(trade_log or [], "net_pnl")
    n = len(net)
    if n < MIN_TRADES:
        return {"verdict": "INSUFFICIENT_DATA", "n_trades": n,
                "message": f"Only {n} trades — at least {MIN_TRADES} are needed before any conclusion about an edge."}
    st = _stats(net, seed)
    gross = _r_multiples(trade_log, "gross_pnl")
    g = _stats(gross, seed) if len(gross) == n else None
    lo, hi = st["ci95_low"], st["ci95_high"]
    verdict = "POSITIVE_EXPECTANCY" if lo > 0 else "NEGATIVE_EXPECTANCY" if hi < 0 else "NO_DEMONSTRATED_EDGE"
    msg = {
        "POSITIVE_EXPECTANCY": "The average trade is positive and the 95 % interval excludes zero.",
        "NEGATIVE_EXPECTANCY": "The average trade LOSES money and the 95 % interval excludes zero: this is not bad luck. Do not trade this with real money.",
        "NO_DEMONSTRATED_EDGE": "The 95 % interval includes zero: the result cannot be distinguished from luck.",
    }[verdict]
    return {"verdict": verdict, "n_trades": n, **st, "gross_mean_r": g["mean_r"] if g else None,
            "gross_ci95_low": g["ci95_low"] if g else None, "gross_ci95_high": g["ci95_high"] if g else None,
            "message": msg,
            "caveats": "Trades are treated as independent and one backtest period is a single sample of the market."}
