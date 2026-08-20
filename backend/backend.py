from __future__ import annotations

from pathlib import Path

# This module exists to support Render deployments when the service Root
# Directory is configured as `backend/`. In that case, Python's import path
# starts from the backend folder itself, but many modules use absolute
# imports like `backend.config.settings`.
#
# By exposing `__path__`, this module becomes a package-like entrypoint and
# allows imports such as `backend.config` to resolve to the current backend
# root directory.

__path__ = [str(Path(__file__).resolve().parent)]
