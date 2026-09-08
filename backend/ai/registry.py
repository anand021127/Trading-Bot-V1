"""Model versioning.

Every trained model is written as:
  models/ai/<model_version>/model.pkl
  models/ai/<model_version>/metadata.json
  models/ai/latest.json   <- pointer {"model_version": "..."}  (only file ever overwritten)

`metadata.json` always records: version, training period, validation
period, feature list, target definition, model type/params, validation
metrics, and creation timestamp — per PHASE 12. Nothing is silently
overwritten; a new training run always gets a new version directory.
"""
from __future__ import annotations

import json
import pickle
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.ai.features import FEATURE_NAMES
from backend.ai.model import TrainedModel

DEFAULT_MODELS_DIR = Path("models/ai")


@dataclass
class ModelMetadata:
    model_version: str
    model_type: str
    created_at: str
    training_months: List[str]
    validation_month: str
    test_months: List[str]
    feature_list: List[str]
    target_definition: str
    val_auc: Optional[float]
    val_log_loss: Optional[float]
    val_brier: Optional[float]
    notes: str = ""


def save_model(
    trained: TrainedModel,
    training_months: List[str],
    validation_month: str,
    test_months: List[str],
    notes: str = "",
    models_dir: Path = DEFAULT_MODELS_DIR,
) -> ModelMetadata:
    version = datetime.now(timezone.utc).strftime("v%Y%m%d_%H%M%S")
    version_dir = models_dir / version
    version_dir.mkdir(parents=True, exist_ok=True)

    with open(version_dir / "model.pkl", "wb") as f:
        pickle.dump(trained.model, f)

    meta = ModelMetadata(
        model_version=version,
        model_type=trained.model_type,
        created_at=datetime.now(timezone.utc).isoformat(),
        training_months=training_months,
        validation_month=validation_month,
        test_months=test_months,
        feature_list=list(FEATURE_NAMES),
        target_definition=(
            "1 if 2R target (3x entry-ATR) is hit before 1.5x-ATR stop within "
            "48 bars (4h) of the decision bar, else 0 (stop-first or timeout)."
        ),
        val_auc=trained.val_auc if trained.val_auc == trained.val_auc else None,  # NaN check
        val_log_loss=trained.val_log_loss,
        val_brier=trained.val_brier,
        notes=notes,
    )
    with open(version_dir / "metadata.json", "w") as f:
        json.dump(asdict(meta), f, indent=2)

    with open(models_dir / "latest.json", "w") as f:
        json.dump({"model_version": version}, f, indent=2)

    return meta


def load_latest_model(models_dir: Path = DEFAULT_MODELS_DIR) -> Optional[Dict[str, Any]]:
    """Returns {"model": <predict_proba-capable object>, "metadata": ModelMetadata-as-dict}
    or None if no model has been trained/saved yet. Never raises — callers
    (the live/backtest predictor) must fail safe if this returns None."""
    try:
        latest_path = models_dir / "latest.json"
        if not latest_path.exists():
            return None
        with open(latest_path) as f:
            version = json.load(f)["model_version"]
        version_dir = models_dir / version
        with open(version_dir / "model.pkl", "rb") as f:
            model = pickle.load(f)
        with open(version_dir / "metadata.json") as f:
            metadata = json.load(f)
        return {"model": model, "metadata": metadata}
    except Exception:
        return None
