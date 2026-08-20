"""Unit tests for the backend config loader."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from backend.config.loader import ConfigLoader, load_config


class TestConfigLoader:
    """Tests for the config loader implementation."""

    def test_load_config_reads_environment_variables(self) -> None:
        """Environment variables should be loaded into the config mapping."""
        with mock.patch.dict(os.environ, {"UPSTOX_API_KEY": "abc", "UPSTOX_API_SECRET": "def"}, clear=False):
            config = load_config()

        assert config["UPSTOX_API_KEY"] == "abc"
        assert config["UPSTOX_API_SECRET"] == "def"

    def test_load_config_reads_dotenv_file(self) -> None:
        """A .env file should be parsed when present."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dotenv_path = Path(tmpdir) / ".env"
            dotenv_path.write_text("UPSTOX_API_KEY=from-file\nUPSTOX_API_SECRET=from-secret\n", encoding="utf-8")

            with mock.patch("backend.config.loader.Path.cwd", return_value=Path(tmpdir)):
                config = load_config()

        assert config["UPSTOX_API_KEY"] == "from-file"
        assert config["UPSTOX_API_SECRET"] == "from-secret"

    def test_load_config_prefers_environment_over_file(self) -> None:
        """Explicit environment variables should override values from the .env file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dotenv_path = Path(tmpdir) / ".env"
            dotenv_path.write_text("UPSTOX_API_KEY=file-value\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"UPSTOX_API_KEY": "env-value"}, clear=False), mock.patch(
                "backend.config.loader.Path.cwd", return_value=Path(tmpdir)
            ):
                config = load_config()

        assert config["UPSTOX_API_KEY"] == "env-value"

    def test_config_loader_requires_existing_file(self) -> None:
        """The loader should raise when the provided .env path does not exist."""
        loader = ConfigLoader(dotenv_path=Path("/definitely/missing/.env"))

        with pytest.raises(FileNotFoundError):
            loader.load()

    def test_config_loader_defaults_to_repo_root(self) -> None:
        """The loader should target the repository root by default when no path is specified."""
        loader = ConfigLoader()

        assert loader.dotenv_path == Path.cwd() / ".env"
