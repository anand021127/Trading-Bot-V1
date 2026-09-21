"""Model registry stubs for the optional AI layer (disabled by default)."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


def load_latest_model(models_dir: str | Path | None = None) -> Optional[Any]:
    """Return None when no trained model is present — fail-open path."""
    if models_dir is None:
        return None
    path = Path(models_dir)
    if not path.exists():
        return None
    return None
