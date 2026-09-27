"""PHASE 5.3C — Copilot Strategy Context tests.

Pins:
  1. The static parameter table matches the REAL V8DStrategy class (no drift).
  2. The context contains everything Copilot needs (params, entry policy,
     underlyings, option-selection rules, risk, AI status, scan state,
     backtest comparison) — never "I don't have information about V8-D".
  3. NO secrets anywhere in the payload (token/key/credential patterns).
  4. Missing data is reported honestly (exact missing file), never invented.
  5. build_context() assembles strategy_context for every question type.
"""
from __future__ import annotations

import json
import re

from backend.copilot.strategy_context import (
    BACKTEST_COMPARISON,
    STRATEGY_NAME,
    V8D_PARAMS,
    build_strategy_context,
)


def _walk_strings(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)
    elif isinstance(obj, str):
        yield obj


class TestParameterConsistency:
    def test_static_table_matches_real_strategy_class(self):
        from backend.strategy.strategies.v8d_strategy import V8DStrategy
        s = V8DStrategy()
        for k, v in V8D_PARAMS.items():
            actual = getattr(s, k)
            assert actual == v, f"strategy_context.{k}={v} but V8DStrategy.{k}={actual}"

    def test_build_strategy_context_uses_live_class_values(self):
        ctx = build_strategy_context()
        assert ctx["strategy"]["parameters"]["max_account_risk_pct"] == 0.025
        assert ctx["strategy"]["parameters"]["atr_stop_mult"] == 1.8
        assert ctx["strategy"]["name"] == STRATEGY_NAME == "V8_D_PULLBACK_ATM"


class TestContextCompleteness:
    def test_all_required_sections_present(self):
        ctx = build_strategy_context()
        assert ctx["strategy"]["entry_policy"]["last_entry_ist"] == "14:45"
        assert ctx["strategy"]["entry_policy"]["cutoff_inclusive"] is True
        assert set(ctx["strategy"]["supported_underlyings"]) == {
            "NIFTY50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}
        assert ctx["strategy"]["option_selection_rules"]
        assert "1.5" in " ".join(ctx["strategy"]["stop_target_rules"])
        assert "max_daily_trades" in ctx["risk_state"]
        assert "effective_enabled" in ctx["ai_status"]
        assert "fail_closed" in ctx["ai_status"]
        assert "recent_scan" in ctx
        assert "backtest_comparison" in ctx

    def test_backtest_comparison_has_both_runs_with_rejections(self):
        prev = BACKTEST_COMPARISON["previous_run_2026_09_26_engine"]
        latest = BACKTEST_COMPARISON["latest_run_2026_09_27_engine"]
        assert prev["trades"] == 277 and prev["net_pnl"] == -81511.32
        assert latest["trades"] == 14 and latest["net_pnl"] == -2897.00
        assert latest["dominant_rejections"]["Daily trade limit reached: 3/3"] == 43590
        assert "unchanged_between_runs" in BACKTEST_COMPARISON

    def test_scan_state_honest_when_file_missing(self):
        ctx = build_strategy_context()
        # On a machine without the scan-state file the section must say so —
        # never fabricate a scan result.
        rs = ctx["recent_scan"]
        if not rs.get("available"):
            assert rs.get("missing"), "missing-data reason must be stated"


class TestNoSecrets:
    def test_no_token_or_credential_patterns(self):
        ctx = build_strategy_context()
        blob = "\n".join(_walk_strings(ctx))
        patterns = [
            r"UPSTOX_ACCESS_TOKEN\s*=", r"LTpk[A-Za-z0-9]{10,}",
            r"sk-[A-Za-z0-9]{16,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
            r"Bearer\s+[A-Za-z0-9._\-]{20,}", r"CONTROL_TOKEN\s*=\s*\S",
            r"api[_-]?key\s*[=:]\s*['\"]?[A-Za-z0-9]{16,}",
        ]
        for p in patterns:
            assert not re.search(p, blob, re.IGNORECASE), f"secret pattern leaked: {p}"

    def test_payload_json_serializable_and_bounded(self):
        ctx = build_strategy_context()
        blob = json.dumps(ctx, default=str)
        assert len(blob) < 200_000  # bounded context, not a DB dump


class TestContextAssembly:
    def test_build_context_includes_strategy_context(self):
        from unittest.mock import MagicMock
        from backend.copilot.context import build_context
        tools = MagicMock()
        out = build_context("Is V8-D too restrictive?", tools)
        assert "strategy_context" in out
        assert out["strategy_context"]["strategy"]["name"] == "V8_D_PULLBACK_ATM"

    def test_trades_question_also_carries_strategy_context(self):
        from unittest.mock import MagicMock
        from backend.copilot.context import build_context
        tools = MagicMock()
        out = build_context("Why didn't we trade today?", tools)
        assert "strategy_context" in out
        assert out["strategy_context"]["ai_status"].get("fail_closed")
