"""Thin REST client wrapper for the Upstox API.

This module centralizes request construction for the broker integration layer so
other modules can call into Upstox with a consistent interface.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import requests


class UpstoxClient:
    """Small wrapper around the Upstox REST API."""

    def __init__(self, access_token: Optional[str] = None, base_url: str = "https://api.upstox.com") -> None:
        self.access_token = access_token or os.getenv("UPSTOX_ACCESS_TOKEN")
        self.base_url = base_url.rstrip("/")

    def _headers(self) -> Dict[str, str]:
        """Build the request headers for authenticated calls."""
        headers = {"Content-Type": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Perform a GET request against the Upstox API."""
        url = f"{self.base_url}{path}"
        response = requests.get(url, params=params, headers=self._headers(), timeout=10)
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        return response.json()
