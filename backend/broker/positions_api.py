"""Distinguish broker API failure from a genuine flat book."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional


class BrokerPositionsError(RuntimeError):
    pass


@dataclass
class PositionsResult:
    ok: bool
    positions: List[dict]
    error: Optional[str] = None


def fetch_positions(client: Any) -> PositionsResult:
    if client is None:
        return PositionsResult(ok=False, positions=[], error="no_client")
    getter = getattr(client, "get_positions_with_details", None) or getattr(client, "get_positions", None)
    if getter is None:
        return PositionsResult(ok=False, positions=[], error="client_missing_get_positions")
    try:
        data = getter()
    except Exception as exc:
        return PositionsResult(ok=False, positions=[], error=type(exc).__name__)
    if data is None:
        return PositionsResult(ok=False, positions=[], error="null_response")
    if not isinstance(data, list):
        return PositionsResult(ok=False, positions=[], error="invalid_payload")
    return PositionsResult(ok=True, positions=data, error=None)
