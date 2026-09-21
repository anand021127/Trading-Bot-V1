from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from backend.ai.features import FEATURE_NAMES, build_feature_vector
from backend.strategy.confidence_scoring import ConfidenceScorer


@dataclass
class DatasetRow:
    timestamp: str
    features: Dict[str, float]
    label: float
    index: int


@dataclass
class DatasetReport:
    rows_emitted: int = 0
    rows_skipped_no_horizon: int = 0
    rows_skipped_no_setup: int = 0


def _label_forward(candles: List[Dict[str, Any]], i: int, horizon_bars: int, direction: str) -> float | None:
    if i + horizon_bars >= len(candles):
        return None
    now = float(candles[i]["close"])
    fut = float(candles[i + horizon_bars]["close"])
    if now <= 0:
        return None
    chg = (fut - now) / now
    if direction == "CE":
        return 1.0 if chg > 0 else 0.0
    return 1.0 if chg < 0 else 0.0


def build_dataset(
    candles: List[Dict[str, Any]],
    symbol: str = "",
    horizon_bars: int = 20,
    warmup_bars: int = 60,
) -> Tuple[List[DatasetRow], DatasetReport]:
    scorer = ConfidenceScorer()
    rows: List[DatasetRow] = []
    report = DatasetReport()
    for i in range(warmup_bars, len(candles)):
        window = candles[: i + 1]
        setup = scorer.evaluate(window)
        fv = build_feature_vector(candles[i], setup)
        if fv is None:
            report.rows_skipped_no_setup += 1
            continue
        label = _label_forward(candles, i, horizon_bars, setup.direction)
        if label is None:
            report.rows_skipped_no_horizon += 1
            continue
        rows.append(DatasetRow(timestamp=str(candles[i].get("timestamp")), features=fv, label=label, index=i))
    report.rows_emitted = len(rows)
    return rows, report
