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


class ConfigLoader:
    """Load configuration values from the environment and a .env file.

    Parameters
    ----------
    dotenv_path:
        Optional path to the .env file. When omitted, the loader uses the
        current working directory plus ".env".
    """

    def __init__(self, dotenv_path: Optional[Path] = None) -> None:
        self.dotenv_path = dotenv_path or Path.cwd() / ".env"

    def _parse_dotenv(self, path: Path) -> Dict[str, str]:
        """Parse a simple .env file into a dictionary of keys and values."""
        values: Dict[str, str] = {}
        if not path.exists():
            return values

        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")

        return values

    def load(self) -> Dict[str, str]:
        """Load configuration values from the environment and the .env file.

        Environment variables always take precedence over values declared in the
        .env file.
        """
        if not self.dotenv_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.dotenv_path}")

        config: Dict[str, str] = {}
        config.update(self._parse_dotenv(self.dotenv_path))
        for key, value in os.environ.items():
            config[key] = value

        return config


def load_config(dotenv_path: Optional[Path] = None) -> Dict[str, str]:
    """Convenience helper that instantiates and runs the config loader."""
    return ConfigLoader(dotenv_path=dotenv_path).load()
