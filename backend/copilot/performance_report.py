"""PHASE 4: shadow performance report.

Reads backend/copilot/shadow_logger.py's CSV (after reconciliation.py has
filled in hypothetical_outcome) and computes the requested stats. This
module ONLY reports — it does not feed back into, tune, or select any
Copilot threshold. R-multiples use each row's own logged risk_reward
where available; where not, a 1R loss / target-implied-R win is assumed
consistently (documented below), matching the same convention used in
backend/ai/walk_forward.py so the two are comparable.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.copilot.shadow_logger import DEFAULT_LOG_PATH

RESOLVED_OUTCOMES = ("TARGET_HIT", "SL_HIT", "TIMEOUT")


@dataclass
class Bucket:
    label: str
    total: int = 0
    target_hits: int = 0
    sl_hits: int = 0
    timeouts: int = 0
    r_values: List[float] = field(default_factory=list)

    def add(self, outcome: str, r: float) -> None:
        self.total += 1
        if outcome == "TARGET_HIT":
            self.target_hits += 1
        elif outcome == "SL_HIT":
            self.sl_hits += 1
        elif outcome == "TIMEOUT":
            self.timeouts += 1
        self.r_values.append(r)

    def to_dict(self) -> Dict[str, Any]:
        resolved = self.target_hits + self.sl_hits + self.timeouts
        wins = self.target_hits
        gross_profit = sum(r for r in self.r_values if r > 0)
        gross_loss = -sum(r for r in self.r_values if r < 0)
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        streak = 0
        max_streak = 0
        for r in self.r_values:
            equity += r
            peak = max(peak, equity)
            max_dd = min(max_dd, equity - peak)
            if r <= 0:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
        return {
            "label": self.label,
            "total_opportunities": self.total,
            "resolved": resolved,
            "target_hits": self.target_hits,
            "sl_hits": self.sl_hits,
            "timeouts": self.timeouts,
            "win_rate_pct": round(wins / resolved * 100, 2) if resolved else None,
            "net_R": round(sum(self.r_values), 3) if self.r_values else None,
            "average_R": round(sum(self.r_values) / len(self.r_values), 4) if self.r_values else None,
            "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else None,
            "max_drawdown_R": round(max_dd, 3) if self.r_values else None,
            "max_consecutive_losses": max_streak,
        }


def _row_r_value(row: Dict[str, str]) -> Optional[float]:
    """R-multiple for one resolved row. Uses the row's own logged R:R for
    a win (TARGET_HIT); a loss (SL_HIT) is -1R by definition (the stop IS
    the 1R reference); TIMEOUT uses the actual logged prices if present,
    else falls back to 0R (no assumption of profit or loss on an
    unresolved-by-time close)."""
    outcome = row.get("hypothetical_outcome")
    if outcome not in RESOLVED_OUTCOMES:
        return None
    try:
        rr = float(row["risk_reward"]) if row.get("risk_reward") else None
    except ValueError:
        rr = None
    if outcome == "TARGET_HIT":
        return rr if rr is not None else 1.0
    if outcome == "SL_HIT":
        return -1.0
    return 0.0  # TIMEOUT — treated as flat, not guessed as win or loss


def build_shadow_performance_report(log_path: Path = DEFAULT_LOG_PATH) -> Dict[str, Any]:
    if not log_path.exists():
        return {"available": False, "reason": f"No shadow log found at {log_path}."}

    with open(log_path) as f:
        rows = list(csv.DictReader(f))

    overall = Bucket("overall")
    by_side: Dict[str, Bucket] = defaultdict(lambda: Bucket(""))
    by_symbol: Dict[str, Bucket] = defaultdict(lambda: Bucket(""))
    by_regime: Dict[str, Bucket] = defaultdict(lambda: Bucket(""))
    by_setup: Dict[str, Bucket] = defaultdict(lambda: Bucket(""))
    by_hour: Dict[str, Bucket] = defaultdict(lambda: Bucket(""))

    approved = rejected = total_opportunities = 0

    for row in rows:
        total_opportunities += 1
        if str(row.get("validation_approved", "")).lower() == "true":
            approved += 1
        else:
            rejected += 1

        r = _row_r_value(row)
        if r is None:
            continue  # not resolved yet — counted in total_opportunities, not in win-rate/R stats

        outcome = row["hypothetical_outcome"]
        overall.add(outcome, r)

        side = row.get("option_type") or row.get("direction") or "UNKNOWN"
        by_side[side].add(outcome, r)

        symbol = row.get("symbol") or "UNKNOWN"
        by_symbol[symbol].add(outcome, r)

        regime = row.get("market_regime") or "UNKNOWN"
        by_regime[regime].add(outcome, r)

        setup = row.get("strategy_confirmation") or "UNKNOWN"
        by_setup[setup].add(outcome, r)

        hour = "UNKNOWN"
        ts = row.get("timestamp", "")
        if len(ts) >= 13 and ts[10] in ("T", " "):
            hour = ts[11:13] + ":00"
        by_hour[hour].add(outcome, r)

    def _dictify(buckets: Dict[str, Bucket]) -> Dict[str, Any]:
        out = {}
        for k, b in buckets.items():
            b.label = k
            out[k] = b.to_dict()
        return out

    return {
        "available": True,
        "log_path": str(log_path),
        "total_opportunities": total_opportunities,
        "approved_opportunities": approved,
        "rejected_opportunities": rejected,
        "overall": overall.to_dict(),
        "by_ce_pe": _dictify(by_side),
        "by_symbol": _dictify(by_symbol),
        "by_market_regime": _dictify(by_regime),
        "by_strategy_setup": _dictify(by_setup),
        "by_hour_of_day": _dictify(by_hour),
        "notes": (
            "R-multiples: TARGET_HIT uses the row's own logged risk_reward "
            "(fallback 1.0R if missing), SL_HIT is always -1.0R by "
            "definition, TIMEOUT is treated as 0R (flat) rather than "
            "guessed. 'volatility' and 'strike selection type' breakdowns "
            "are not in this report — the shadow log doesn't currently "
            "record either field (see docs/COPILOT.md limitations). "
            "Nothing in this module tunes or selects Copilot parameters "
            "based on these results."
        ),
    }
