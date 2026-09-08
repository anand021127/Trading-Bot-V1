"""Chronological walk-forward validation comparing:
    A) EXISTING STRATEGY (ConfidenceScorer signal, confidence >= threshold)
    B) EXISTING STRATEGY + AI FILTER (also requires calibrated AI
       trade-success probability >= AI_MIN_TRADE_PROBABILITY)

No random splits. No test-set tuning. Model selection happens only on
each fold's validation month. Test months are used exactly once, for
reporting, never for fitting or threshold selection.

Outcomes are expressed in R-multiples (R = 1.5x ATR at entry, the same
stop distance backend/strategy/strategies/ema_trend.py already uses) —
NOT real INR P&L. Full-year options premium history isn't available in
this repo (real_data/options_cache only has a handful of Oct-2024 sample
days), so this validates the DIRECTIONAL edge the AI filter adds to the
underlying-move call, which is what an options-buying strategy's
stop/target actually depends on. This scope limit is intentional and
disclosed rather than papered over with a fabricated premium model.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from backend.ai.dataset import DatasetRow, build_dataset, rows_to_xy
from backend.ai.model import TrainedModel, predict_proba_row, select_best_model

MONTHS_2024 = [f"2024-{m:02d}" for m in range(1, 13)]


def _month_of(ts: str) -> str:
    return ts[:7] if ts else ""


@dataclass
class FoldResult:
    test_month: str
    train_months: List[str]
    val_month: str
    model_type: str
    val_auc: float
    n_train: int
    n_val: int
    n_test: int


@dataclass
class TradeStats:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_profit_r: float = 0.0
    gross_loss_r: float = 0.0
    max_consec_losses: int = 0
    equity_curve_r: List[float] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def net_r(self) -> float:
        return self.gross_profit_r - self.gross_loss_r

    @property
    def profit_factor(self) -> Optional[float]:
        return (self.gross_profit_r / self.gross_loss_r) if self.gross_loss_r > 0 else None

    @property
    def expectancy_r(self) -> float:
        return self.net_r / self.trades if self.trades else 0.0

    @property
    def max_drawdown_r(self) -> float:
        peak = 0.0
        cum = 0.0
        max_dd = 0.0
        for r in self.equity_curve_r:
            cum += r
            peak = max(peak, cum)
            max_dd = min(max_dd, cum - peak)
        return max_dd

    def to_report_dict(self) -> Dict[str, Any]:
        return {
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate_pct": round(self.win_rate * 100, 2),
            "gross_profit_R": round(self.gross_profit_r, 2),
            "gross_loss_R": round(self.gross_loss_r, 2),
            "net_R": round(self.net_r, 2),
            "profit_factor": round(self.profit_factor, 3) if self.profit_factor is not None else None,
            "expectancy_R": round(self.expectancy_r, 4),
            "max_drawdown_R": round(self.max_drawdown_r, 2),
            "max_consecutive_losses": self.max_consec_losses,
        }


def _accumulate(stats: TradeStats, row: DatasetRow, r_win: float = 2.0, r_loss: float = 1.0) -> None:
    r = r_win if row.label == 1 else -r_loss
    stats.trades += 1
    if row.label == 1:
        stats.wins += 1
        stats.gross_profit_r += r_win
    else:
        stats.losses += 1
        stats.gross_loss_r += r_loss
    stats.equity_curve_r.append(r)


def _consecutive_losses(rows: List[DatasetRow]) -> int:
    streak = 0
    worst = 0
    for row in rows:
        if row.label == 0:
            streak += 1
            worst = max(worst, streak)
        else:
            streak = 0
    return worst


def run_walk_forward(
    candles: List[Dict[str, Any]],
    symbol: str,
    min_tradeable_confidence: float = 70.0,
    ai_min_probability: float = 0.65,
    test_months: Optional[List[str]] = None,
    horizon_bars: int = 48,
) -> Dict[str, Any]:
    from backend.ai.dataset import select_non_overlapping
    from backend.ai.features import feature_vector_to_row

    all_rows, build_report = build_dataset(candles, symbol=symbol, horizon_bars=horizon_bars)
    print(f"[walk_forward] dataset built: {build_report}")

    if test_months is None:
        test_months = MONTHS_2024[7:12]  # Aug..Dec 2024 — 5 unseen-future folds

    rows_by_month: Dict[str, List[DatasetRow]] = {}
    for r in all_rows:
        rows_by_month.setdefault(_month_of(r.timestamp), []).append(r)

    fold_results: List[FoldResult] = []
    baseline_stats = TradeStats()
    ai_stats = TradeStats()
    baseline_rows_all: List[DatasetRow] = []
    ai_kept_rows_all: List[DatasetRow] = []

    # Diagnostics: for every baseline-eligible, non-overlapping trade, record
    # the AI probability and whether the AI would have kept it, so we can
    # directly compare the outcome distribution of accepted vs rejected
    # trades rather than just trusting the aggregate PF numbers.
    diagnostic_rows: List[Dict[str, Any]] = []

    for test_month in test_months:
        idx = MONTHS_2024.index(test_month)
        if idx < 2:
            continue
        val_month = MONTHS_2024[idx - 1]
        train_months = MONTHS_2024[:idx - 1]

        train_rows = [r for m in train_months for r in rows_by_month.get(m, [])]
        val_rows = rows_by_month.get(val_month, [])
        test_rows_raw = rows_by_month.get(test_month, [])

        if len(train_rows) < 50 or len(val_rows) < 20 or not test_rows_raw:
            print(f"[walk_forward] skipping {test_month}: insufficient rows "
                  f"(train={len(train_rows)}, val={len(val_rows)}, test={len(test_rows_raw)})")
            continue

        X_train, y_train = rows_to_xy(train_rows)
        X_val, y_val = rows_to_xy(val_rows)
        trained = select_best_model(X_train, y_train, X_val, y_val)

        fold_results.append(FoldResult(
            test_month=test_month, train_months=train_months, val_month=val_month,
            model_type=trained.model_type, val_auc=round(trained.val_auc, 4) if trained.val_auc == trained.val_auc else None,
            n_train=len(train_rows), n_val=len(val_rows), n_test=len(test_rows_raw),
        ))

        # BASELINE: one open position at a time, gated only by existing
        # confidence threshold — mirrors how RiskManager actually gates entries.
        baseline_month_trades = select_non_overlapping(
            test_rows_raw, horizon_bars,
            accept_fn=lambda r: r.features.get("setup_confidence", 0.0) >= min_tradeable_confidence,
        )
        for row in baseline_month_trades:
            baseline_rows_all.append(row)
            _accumulate(baseline_stats, row)

            proba = predict_proba_row(trained, feature_vector_to_row(row.features))
            diagnostic_rows.append({
                "test_month": test_month, "timestamp": row.timestamp, "direction": row.direction,
                "setup_confidence": row.features.get("setup_confidence"), "ai_probability": round(proba, 4),
                "label": row.label, "future_return_pct": round(row.future_return_pct, 3),
            })
            if proba >= ai_min_probability:
                ai_kept_rows_all.append(row)

        # AI-FILTERED: also one open position at a time, but the position
        # can ONLY open on a bar where the AI additionally clears the bar —
        # this is evaluated as its own independent non-overlapping sequence
        # (not "baseline trades minus the ones AI rejected"), because
        # skipping a baseline trade the AI rejected can free up an earlier
        # entry into the NEXT eligible signal — same as how a real risk
        # manager would behave if that trade had simply never been taken.
        def _ai_accept(r: DatasetRow, _trained=trained) -> bool:
            if r.features.get("setup_confidence", 0.0) < min_tradeable_confidence:
                return False
            p = predict_proba_row(_trained, feature_vector_to_row(r.features))
            return p >= ai_min_probability

        ai_month_trades = select_non_overlapping(test_rows_raw, horizon_bars, accept_fn=_ai_accept)
        for row in ai_month_trades:
            _accumulate(ai_stats, row)

    baseline_stats.max_consec_losses = _consecutive_losses(baseline_rows_all)
    ai_stats.max_consec_losses = _consecutive_losses(ai_kept_rows_all)

    # Accepted-vs-rejected diagnostic (computed on the baseline non-
    # overlapping trade sequence, i.e. "of the trades the existing strategy
    # would actually take one at a time, which ones did the AI like/dislike
    # and were the ones it disliked actually worse?")
    accepted = [d for d in diagnostic_rows if d["ai_probability"] >= ai_min_probability]
    rejected = [d for d in diagnostic_rows if d["ai_probability"] < ai_min_probability]

    def _bucket_stats(bucket: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not bucket:
            return {"n": 0, "win_rate_pct": None, "mean_ai_probability": None, "mean_setup_confidence": None}
        wins = sum(1 for d in bucket if d["label"] == 1)
        return {
            "n": len(bucket),
            "win_rate_pct": round(wins / len(bucket) * 100, 2),
            "mean_ai_probability": round(sum(d["ai_probability"] for d in bucket) / len(bucket), 4),
            "mean_setup_confidence": round(sum(d["setup_confidence"] for d in bucket) / len(bucket), 2),
        }

    diagnostics = {
        "total_baseline_eligible_trades_evaluated_by_ai": len(diagnostic_rows),
        "accepted_by_ai": _bucket_stats(accepted),
        "rejected_by_ai": _bucket_stats(rejected),
        "interpretation_note": (
            "If the AI is adding real signal, 'accepted_by_ai' should show a "
            "meaningfully higher win_rate_pct than 'rejected_by_ai'. If the two "
            "win rates are close (within noise for these sample sizes) and/or "
            "'rejected_by_ai' is nearly empty, the model isn't discriminating — "
            "it's applying a near-constant threshold that happens to reject "
            "almost everything, not learning which setups are better."
        ),
    }

    return {
        "symbol": symbol,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "methodology_note": (
            "Trade counts are DE-DUPLICATED to one open position at a time per "
            "symbol (matching RiskManager's max_simultaneous_positions gating), "
            "not one 'trade' per 5-min bar the setup remains active — see "
            "backend/ai/dataset.py:select_non_overlapping."
        ),
        "dataset_build_report": asdict(build_report),
        "folds": [asdict(f) for f in fold_results],
        "baseline": baseline_stats.to_report_dict(),
        "ai_filtered": ai_stats.to_report_dict(),
        "trades_filtered_out_by_ai": baseline_stats.trades - ai_stats.trades,
        "accepted_vs_rejected_diagnostics": diagnostics,
        "config": {
            "min_tradeable_confidence": min_tradeable_confidence,
            "ai_min_probability": ai_min_probability,
            "horizon_bars": horizon_bars,
            "r_win": 2.0,
            "r_loss": 1.0,
            "note": "R-multiples on the underlying move (1.5x ATR stop / 2R target, "
                    "matching backend/strategy/strategies/ema_trend.py), not real INR "
                    "options premium P&L — see module docstring.",
        },
    }
