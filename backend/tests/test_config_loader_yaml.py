"""Tests for the YAML-backed config loader."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from backend.config.loader import ConfigLoader, load_config


def test_load_config_reads_yaml_settings_and_env() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        settings_path = Path(tmpdir) / "settings.yaml"
        settings_path.write_text("foo: bar\nnumber: 42\n", encoding="utf-8")

        dotenv_path = Path(tmpdir) / ".env"
        dotenv_path.write_text("UPSTOX_API_KEY=from-dotenv\n", encoding="utf-8")

        with mock.patch("backend.config.loader.Path.cwd", return_value=Path(tmpdir)):
            config = load_config(settings_path=settings_path, dotenv_path=dotenv_path)

    assert config["foo"] == "bar"
    assert config["number"] == 42
    assert config["env"]["UPSTOX_API_KEY"] == "from-dotenv"


def test_env_overrides_dotenv_values() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        settings_path = Path(tmpdir) / "settings.yaml"
        settings_path.write_text("foo: bar\n", encoding="utf-8")

        dotenv_path = Path(tmpdir) / ".env"
        dotenv_path.write_text("UPSTOX_API_KEY=from-dotenv\n", encoding="utf-8")

        with mock.patch.dict(os.environ, {"UPSTOX_API_KEY": "from-env"}, clear=False), mock.patch(
            "backend.config.loader.Path.cwd", return_value=Path(tmpdir)
        ):
            config = load_config(settings_path=settings_path, dotenv_path=dotenv_path)

    assert config["env"]["UPSTOX_API_KEY"] == "from-env"


def test_missing_settings_yaml_raises() -> None:
    loader = ConfigLoader(settings_path=Path("/missing/settings.yaml"))
    with pytest.raises(FileNotFoundError):
        loader.load()
