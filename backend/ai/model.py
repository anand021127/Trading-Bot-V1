"""Model training and calibration.

Uses scikit-learn's HistGradientBoostingClassifier — one of the models
explicitly approved for this project (tabular, lightweight, no paid API,
no GPU requirement, ships with the standard scientific Python stack
already used elsewhere in this repo). Candidate models are compared on
the validation split; nothing is chosen on test-set performance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
try:
    from sklearn.frozen import FrozenEstimator
except ImportError:  # sklearn < 1.6 fallback
    FrozenEstimator = None
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score


@dataclass
class TrainedModel:
    model: Any                  # calibrated, sklearn-compatible predict_proba
    model_type: str
    val_auc: float
    val_log_loss: float
    val_brier: float


CANDIDATES = {
    "logistic_regression": lambda: LogisticRegression(max_iter=2000, class_weight="balanced"),
    "random_forest": lambda: RandomForestClassifier(
        n_estimators=300, max_depth=6, min_samples_leaf=20, class_weight="balanced", random_state=42
    ),
    "hist_gradient_boosting": lambda: HistGradientBoostingClassifier(
        max_depth=4, learning_rate=0.05, max_iter=300, l2_regularization=1.0, random_state=42
    ),
}


def _fit_and_calibrate(build_fn, X_train, y_train, X_val, y_val) -> Tuple[Any, float, float, float]:
    base = build_fn()
    base.fit(X_train, y_train)

    # Calibrate on the validation split (never on train, never on test) so
    # "probability" outputs are actually meaningful, not raw scores.
    # sklearn >=1.6 replaced cv="prefit" with wrapping the already-fitted
    # estimator in FrozenEstimator so CalibratedClassifierCV treats it as
    # fixed and only fits the calibration map on X_val/y_val.
    if FrozenEstimator is not None:
        calibrated = CalibratedClassifierCV(FrozenEstimator(base), method="isotonic")
    else:
        calibrated = CalibratedClassifierCV(base, method="isotonic", cv="prefit")
    calibrated.fit(X_val, y_val)

    proba = calibrated.predict_proba(X_val)[:, 1]
    auc = roc_auc_score(y_val, proba) if len(set(y_val)) > 1 else float("nan")
    ll = log_loss(y_val, proba, labels=[0, 1])
    brier = brier_score_loss(y_val, proba)
    return calibrated, auc, ll, brier


def select_best_model(
    X_train: List[List[float]],
    y_train: List[int],
    X_val: List[List[float]],
    y_val: List[int],
) -> TrainedModel:
    """Fits every candidate, calibrates on validation, and picks the one
    with the best validation AUC (ties broken by lower Brier score).
    Model choice happens here — on validation only, never on test."""
    X_train_a, y_train_a = np.array(X_train), np.array(y_train)
    X_val_a, y_val_a = np.array(X_val), np.array(y_val)

    results: List[TrainedModel] = []
    for name, build_fn in CANDIDATES.items():
        try:
            calibrated, auc, ll, brier = _fit_and_calibrate(build_fn, X_train_a, y_train_a, X_val_a, y_val_a)
            results.append(TrainedModel(model=calibrated, model_type=name, val_auc=auc, val_log_loss=ll, val_brier=brier))
        except Exception as e:  # a candidate failing to fit shouldn't kill the run
            print(f"[ai.model] candidate '{name}' failed to fit/calibrate: {e}")

    if not results:
        raise RuntimeError("No candidate model could be trained — check dataset size/class balance.")

    results.sort(key=lambda r: (-(r.val_auc if r.val_auc == r.val_auc else -1.0), r.val_brier))
    return results[0]


def predict_proba_row(trained: TrainedModel, feature_row: List[float]) -> float:
    return float(trained.model.predict_proba(np.array([feature_row]))[0, 1])
