"""Builds a leakage-safe (X, y) training dataset from real historical candles.

Design:
  - Walk the candle series bar-by-bar, in order.
  - At each bar t (once enough warmup history exists), call the SAME
    `ConfidenceScorer.evaluate(candles[:t+1])` the live/backtest engines
    call, so the "existing strategy's opinion" the AI is trained to filter
    is the real thing, not a re-implementation.
  - X(t) = backend.ai.features.build_feature_vector(...) — built ONLY from
    candles[:t+1] and the setup_result it produced. No candle at or after
    t+1 is touched while building X.
  - Y(t) is decided by scanning STRICTLY FORWARD from t+1: using the same
    ATR-based stop (1.5x ATR) / target (2R = 3x ATR) convention the
    existing EMATrendStrategy already uses (see
    backend/strategy/strategies/ema_trend.py), walk forward up to
    `horizon_bars` candles and record whichever is touched first:
      1 -> target hit before stop  (existing strategy's trade would have won)
      0 -> stop hit first, OR neither hit within the horizon (timeout = no
           realized edge, scored as a loss/no-trade outcome)
  - Rows are emitted in chronological order and are NEVER shuffled. The
    caller is responsible for splitting chronologically (see
    backend/ai/walk_forward.py) — this module does not do random splits.

Note on scope: this labels the DIRECTIONAL underlying move (would the
options-buying trade's stop/target have been hit), not a full options-
premium P&L simulation. Full-year options premium history isn't available
in this repo (real_data/options_cache only has a few Oct-2024 sample
days) — see the walk-forward report for how this limitation is handled
and disclosed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from backend.ai.features import FEATURE_NAMES, build_feature_vector
from backend.strategy.confidence_scoring import ConfidenceScorer


@dataclass
class DatasetRow:
    timestamp: str
    symbol: str
    direction: str
    setup_name: str
    features: Dict[str, float]
    label: int              # 1 = target hit first, 0 = stop hit / timeout
    future_return_pct: float  # realized underlying return at label resolution, for reporting only
    decision_index: int = -1  # index into the source candle list — needed to detect overlapping trades


@dataclass
class DatasetBuildReport:
    symbol: str
    total_bars: int
    warmup_bars_skipped: int
    setups_with_direction: int
    rows_emitted: int
    rows_skipped_no_horizon: int  # near end of series, not enough forward bars to resolve label
    positive_rate: float


def _label_forward(
    candles: List[Dict[str, Any]],
    entry_idx: int,
    entry_price: float,
    direction: str,
    atr: float,
    horizon_bars: int,
    atr_stop_mult: float = 1.5,
    atr_target_mult: float = 3.0,  # 2R with a 1.5x ATR stop
) -> Optional[Tuple[int, float]]:
    """Scan candles[entry_idx+1 : entry_idx+1+horizon_bars] (strictly future
    bars relative to the decision) for the first stop/target touch.
    Returns (label, future_return_pct) or None if there isn't enough
    forward data left in the series to resolve the label (those rows are
    dropped, not guessed)."""
    if atr <= 0:
        return None
    end = entry_idx + 1 + horizon_bars
    if end > len(candles):
        return None  # not enough forward bars — drop rather than fabricate

    if direction == "CE":
        stop = entry_price - atr_stop_mult * atr
        target = entry_price + atr_target_mult * atr
    else:  # PE — profits on downside
        stop = entry_price + atr_stop_mult * atr
        target = entry_price - atr_target_mult * atr

    for i in range(entry_idx + 1, end):
        bar = candles[i]
        hi, lo = float(bar["high"]), float(bar["low"])
        if direction == "CE":
            hit_target = hi >= target
            hit_stop = lo <= stop
        else:
            hit_target = lo <= target
            hit_stop = hi >= stop
        if hit_target and hit_stop:
            # Both touched in the same bar — conservative: assume stop first
            # (can't know intrabar order from OHLC alone; never assume the
            # favorable outcome when it's ambiguous).
            return 0, (candles[i]["close"] - entry_price) / entry_price * 100.0
        if hit_target:
            return 1, (target - entry_price) / entry_price * 100.0
        if hit_stop:
            return 0, (stop - entry_price) / entry_price * 100.0

    # Timeout — neither touched within horizon
    close_end = float(candles[end - 1]["close"])
    return 0, (close_end - entry_price) / entry_price * 100.0


def build_dataset(
    candles: List[Dict[str, Any]],
    symbol: str,
    horizon_bars: int = 48,   # 48 x 5min = 4 hours, well inside one session
    warmup_bars: int = 120,
    stride: int = 1,          # evaluate every bar; raise to subsample for speed
    scorer: Optional[ConfidenceScorer] = None,
) -> Tuple[List[DatasetRow], DatasetBuildReport]:
    scorer = scorer or ConfidenceScorer()
    rows: List[DatasetRow] = []
    setups_with_direction = 0
    rows_skipped_no_horizon = 0

    n = len(candles)
    for t in range(warmup_bars, n, stride):
        window = candles[: t + 1]  # ONLY past + current bar — no lookahead
        setup_result = scorer.evaluate(window)
        if setup_result.direction not in ("CE", "PE"):
            continue
        setups_with_direction += 1

        fv = build_feature_vector(candles[t], setup_result)
        if fv is None:
            continue

        entry_price = float(candles[t]["close"])
        atr = float((setup_result.indicators or {}).get("atr", 0.0) or 0.0)

        result = _label_forward(candles, t, entry_price, setup_result.direction, atr, horizon_bars)
        if result is None:
            rows_skipped_no_horizon += 1
            continue
        label, fwd_ret = result

        rows.append(
            DatasetRow(
                timestamp=candles[t].get("timestamp", ""),
                symbol=symbol,
                direction=setup_result.direction,
                setup_name=setup_result.setup_name,
                features=fv,
                label=label,
                future_return_pct=fwd_ret,
                decision_index=t,
            )
        )

    positive_rate = (sum(r.label for r in rows) / len(rows)) if rows else 0.0
    report = DatasetBuildReport(
        symbol=symbol,
        total_bars=n,
        warmup_bars_skipped=warmup_bars,
        setups_with_direction=setups_with_direction,
        rows_emitted=len(rows),
        rows_skipped_no_horizon=rows_skipped_no_horizon,
        positive_rate=round(positive_rate, 4),
    )
    return rows, report


def rows_to_xy(rows: List[DatasetRow]) -> Tuple[List[List[float]], List[int]]:
    from backend.ai.features import feature_vector_to_row
    X = [feature_vector_to_row(r.features) for r in rows]
    y = [r.label for r in rows]
    return X, y


def select_non_overlapping(
    rows: List[DatasetRow],
    horizon_bars: int,
    accept_fn=None,
) -> List[DatasetRow]:
    """Greedily selects rows that don't overlap in time — i.e. simulates
    "only one open position at a time," matching how RiskManager /
    max_simultaneous_positions actually gates entries in this project.

    Without this, every bar the underlying strategy stays in the same
    setup gets counted as an independent "trade" (they are not — they are
    the same real-world position re-evaluated every 5 minutes), which
    massively inflates trade counts and distorts win-rate/PF/expectancy.

    `accept_fn(row) -> bool` is an optional extra gate (e.g. "confidence
    >= 70" or "AI probability >= threshold") checked only on rows that
    are otherwise eligible (no open position). Rows must already be in
    chronological order (build_dataset guarantees this).
    """
    selected: List[DatasetRow] = []
    next_free_index = -1
    for row in rows:
        if row.decision_index < next_free_index:
            continue  # a position would still be open at this bar
        if accept_fn is not None and not accept_fn(row):
            continue  # no open position, but this signal doesn't qualify
        selected.append(row)
        next_free_index = row.decision_index + horizon_bars
    return selected
