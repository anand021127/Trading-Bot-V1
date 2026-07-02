from __future__ import annotations

from pathlib import Path

"""Shim package to support Render deployments when the service root is the backend folder."""

__path__ = [str(Path(__file__).resolve().parent.parent)]
