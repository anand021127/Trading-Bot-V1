"""Environment + YAML configuration loader."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional


class ConfigLoader:
    def __init__(
        self,
        dotenv_path: Optional[Path] = None,
        settings_path: Optional[Path] = None,
    ) -> None:
        self._dotenv_explicit = dotenv_path is not None
        self._settings_explicit = settings_path is not None
        self.dotenv_path = Path(dotenv_path) if dotenv_path is not None else Path.cwd() / ".env"
        self.settings_path = Path(settings_path) if settings_path is not None else Path.cwd() / "settings.yaml"

    def _parse_dotenv(self, path: Path) -> Dict[str, str]:
        values: Dict[str, str] = {}
        if not path.exists():
            return values
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            values[key.strip()] = val.strip().strip('"').strip("'")
        return values

    def _parse_yaml(self, path: Path) -> Dict[str, Any]:
        text = path.read_text(encoding="utf-8")
        try:
            import yaml  # type: ignore
            data = yaml.safe_load(text) or {}
            if not isinstance(data, dict):
                raise ValueError("settings.yaml must be a mapping")
            return data
        except ImportError:
            data: Dict[str, Any] = {}
            for raw in text.splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, val = line.split(":", 1)
                val = val.strip()
                if val.isdigit():
                    data[key.strip()] = int(val)
                else:
                    try:
                        data[key.strip()] = float(val)
                    except ValueError:
                        data[key.strip()] = val
            return data

    def load(
        self,
        settings_path: Optional[Path] = None,
        dotenv_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        dotenv = Path(dotenv_path) if dotenv_path is not None else self.dotenv_path
        settings = Path(settings_path) if settings_path is not None else self.settings_path

        require_dotenv = self._dotenv_explicit and dotenv_path is None
        require_settings = self._settings_explicit and settings_path is None
        if dotenv_path is not None and not dotenv.exists():
            raise FileNotFoundError(str(dotenv))
        if settings_path is not None and not settings.exists():
            raise FileNotFoundError(str(settings))
        if require_dotenv and not dotenv.exists():
            raise FileNotFoundError(str(dotenv))
        if require_settings and not settings.exists():
            raise FileNotFoundError(str(settings))

        file_env = self._parse_dotenv(dotenv) if dotenv.exists() else {}
        yaml_data = self._parse_yaml(settings) if settings.exists() else {}

        merged_env = dict(file_env)
        merged_env.update({k: v for k, v in os.environ.items()})

        config: Dict[str, Any] = dict(yaml_data)
        config.update(merged_env)
        config["env"] = merged_env
        return config


def load_config(
    settings_path: Optional[Path] = None,
    dotenv_path: Optional[Path] = None,
) -> Dict[str, Any]:
    loader = ConfigLoader()
    return loader.load(settings_path=settings_path, dotenv_path=dotenv_path)
