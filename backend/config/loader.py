"""Configuration loading helpers for the backend.

This module provides a lightweight configuration loader that reads values from
environment variables and an optional .env file. It is intentionally small so
it can be used by later trading bot components without pulling in extra
runtime dependencies.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional

import yaml


class ConfigLoader:
    """Load configuration from YAML settings and environment variables.

    This loader reads `settings.yaml` from the backend config directory and also
    merges values from a `.env` file plus process environment variables.
    """

    def __init__(self, settings_path: Optional[Path] = None, dotenv_path: Optional[Path] = None) -> None:
        config_dir = Path(__file__).resolve().parent
        self.settings_path = settings_path or config_dir / "settings.yaml"
        self.dotenv_path = dotenv_path or Path.cwd() / ".env"
        self.dotenv_path_explicit = dotenv_path is not None

    def _parse_dotenv(self, path: Path) -> Dict[str, str]:
        if path is None:
            return {}

        if not path.exists():
            if self.dotenv_path_explicit:
                raise FileNotFoundError(f"Dotenv file not found: {path}")
            return {}

        values: Dict[str, str] = {}
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")

        return values

    def _load_yaml(self, path: Path) -> Dict[str, object]:
        if not path.exists():
            raise FileNotFoundError(f"Settings file not found: {path}")

        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    def load(self) -> Dict[str, object]:
        config: Dict[str, object] = {}
        config.update(self._load_yaml(self.settings_path))

        env_values = self._parse_dotenv(self.dotenv_path)
        env_values.update(os.environ)

        config.update(env_values)
        config["env"] = env_values
        return config


def load_config(settings_path: Optional[Path] = None, dotenv_path: Optional[Path] = None) -> Dict[str, object]:
    return ConfigLoader(settings_path=settings_path, dotenv_path=dotenv_path).load()
