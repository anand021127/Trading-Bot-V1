"""Regression tests for the UNDERLYING QUOTE (live_quote) diagnostics test.

Root cause of the original failure: `_test_live_quote()` used
`ZoneInfo("Asia/Kolkata")` for authoritative session checks but
`backend/api/routers/diagnostics.py` never imported `ZoneInfo`, so the
closed-market branch raised `NameError: name 'ZoneInfo' is not defined`
and the API Test page reported:

    UNDERLYING QUOTE — Error: name 'ZoneInfo' is not defined
"""
from __future__ import annotations

import ast
import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from zoneinfo import ZoneInfo

from backend.api.routers import diagnostics

DIAGNOSTICS_FILE = (
    Path(__file__).resolve().parents[1] / "api" / "routers" / "diagnostics.py"
)


def test_zoneinfo_is_imported_in_diagnostics_module() -> None:
    """The module that references ZoneInfo must import it (static check)."""
    source = DIAGNOSTICS_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_names.update(alias.asname or alias.name for alias in node.names)
    assert "ZoneInfo" in imported_names, (
        "diagnostics.py uses ZoneInfo but does not import it — this is the "
        "root cause of the UNDERLYING QUOTE NameError"
    )
    # And the name must actually resolve in the module namespace.
    assert diagnostics.ZoneInfo is ZoneInfo


def test_zoneinfo_resolves_asia_kolkata() -> None:
    ist = ZoneInfo("Asia/Kolkata")
    assert datetime(2026, 9, 28, 10, 0, tzinfo=ist).utcoffset().total_seconds() == 5.5 * 3600


class _FakeClient:
    """UpstoxClient stand-in that never touches the network."""

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass

    def get_live_quote(self, symbol: str) -> dict:
        return {"ltp": 0, "change_pct": 0}


def test_live_quote_no_nameerror_when_market_closed() -> None:
    """Full regression: run the real live_quote test with the broker mocked
    and the exchange calendar saying 'closed'. The original bug raised
    NameError before reaching the closed-market PASS branch.

    NOTE: `_test_live_quote` imports `exchange_calendar` inside the function
    body, so the patch target is the source module, not `diagnostics`.
    """
    with patch("backend.broker.upstox_client.UpstoxClient", _FakeClient), \
         patch("backend.market.calendar.exchange_calendar") as cal, \
         patch("backend.api.routers.diagnostics.datetime") as fake_dt:
        cal.session_status.return_value = ("BEFORE_OPEN", "pre-market")
        fake_dt.now.return_value = datetime(2026, 9, 28, 9, 0)
        result = asyncio.run(diagnostics._test_live_quote())

    assert result["test_name"] == "live_quote"
    assert result["status"] == "PASS"
    assert "expected" in result["details"].lower()
    assert result["error"] in (None, "")


def test_live_quote_passes_with_valid_ltp() -> None:
    """The happy path must include the session check without raising."""

    class _OpenClient(_FakeClient):
        def get_live_quote(self, symbol: str) -> dict:
            return {"ltp": 24350.5, "change_pct": 0.42}

    with patch("backend.broker.upstox_client.UpstoxClient", _OpenClient), \
         patch("backend.market.calendar.exchange_calendar") as cal, \
         patch("backend.api.routers.diagnostics.datetime") as fake_dt:
        cal.session_status.return_value = ("OPEN", "regular session")
        fake_dt.now.return_value = datetime(2026, 9, 28, 11, 0)
        result = asyncio.run(diagnostics._test_live_quote())

    assert result["test_name"] == "live_quote"
    assert result["status"] == "PASS"
    assert "24350" in result["details"]
