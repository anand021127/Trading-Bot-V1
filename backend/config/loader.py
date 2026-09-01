"""Configuration loading helpers for the backend.

This module provides a lightweight configuration loader that reads values from
environment variables and an optional .env file. It is intentionally small so
it can be used by later trading bot components without pulling in extra
runtime dependencies.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:
    yaml = None


def _parse_simple_yaml(text: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    stack: List[Tuple[int, Any, Optional[str]]] = [(-1, result, None)]
    
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
            
        current_container = stack[-1][1]
        
        if stripped.startswith("- "):
            val = stripped[2:].strip().strip('"').strip("'")
            if isinstance(current_container, list):
                current_container.append(val)
            continue
            
        if ":" in stripped:
            key, rest = stripped.split(":", 1)
            key = key.strip()
            rest = rest.strip()
            
            if not rest:
                # Could be a dict or list coming next
                new_dict: Dict[str, Any] = {}
                if isinstance(current_container, dict):
                    current_container[key] = new_dict
                stack.append((indent, new_dict, key))
            else:
                if rest.lower() == "true":
                    val_parsed: Any = True
                elif rest.lower() == "false":
                    val_parsed = False
                elif rest.startswith('"') and rest.endswith('"'):
                    val_parsed = rest[1:-1]
                elif rest.startswith("'") and rest.endswith("'"):
                    val_parsed = rest[1:-1]
                else:
                    try:
                        val_parsed = int(rest) if "." not in rest else float(rest)
                    except ValueError:
                        val_parsed = rest
                if isinstance(current_container, dict):
                    current_container[key] = val_parsed
    return result


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
            if yaml is not None:
                return yaml.safe_load(fh) or {}
            return _parse_simple_yaml(fh.read())

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
