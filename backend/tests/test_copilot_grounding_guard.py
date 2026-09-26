"""Copilot grounding guard tests (PHASE 5 items 25, 26, 27).

The exact previously-observed failure, now enforced against:
  context: bot HEALTHY, scanner RUNNING, trades_today=0, open_positions=0,
  realized_pnl=0
  → the LLM must NOT say "the bot is executing trades", must NOT invent P&L,
  must NOT claim "I don't have real-time access" while data was provided,
  and must never claim execution authority.
A contradicting answer is DISCARDED and the deterministic explanation is
served. Also proves the trade-plan identity (V8-D, never OPTION_PREMIUM) is
carried into the Copilot decision payload.
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("TRADING_BOT_OFFLINE_TESTS", "1")

from backend.copilot.grounding_guard import (
    check_grounding,
    deterministic_fallback,
    validate_context_freshness,
)


def _healthy_zero_context() -> dict:
    return {
        "bot_health": {"available": True, "overall_status": "HEALTHY"},
        "daily_pnl": {"available": True, "date": "2026-09-26", "trades_today": 0, "realized_pnl": 0.0},
        "open_positions": {"available": True, "positions": []},
        "market_status": {"available": True, "market_open": True, "websocket_connected": True},
    }


class TestFabricatedActivity(unittest.TestCase):
    def test_bot_executing_trades_claim_is_violation(self):
        answer = "The bot is currently executing trades on NIFTY and has open positions."
        v = check_grounding(answer, _healthy_zero_context())
        types = {x["type"] for x in v}
        self.assertIn("FABRICATED_ACTIVITY", types)

    def test_zero_state_honest_answer_passes(self):
        answer = "Today: 0 trades, realized P&L ₹0.00. Open positions: none. The bot is idle."
        self.assertEqual(check_grounding(answer, _healthy_zero_context()), [])

    def test_fabricated_pnl_claim_is_violation(self):
        answer = "The strategy booked a profit of ₹4,500 today."
        v = check_grounding(answer, _healthy_zero_context())
        self.assertIn("FABRICATED_PNL", {x["type"] for x in v})


class TestFalseNoDataDenial(unittest.TestCase):
    def test_no_realtime_access_denial_with_data_is_violation(self):
        answer = "I don't have real-time access to your bot's trades right now."
        v = check_grounding(answer, _healthy_zero_context())
        self.assertIn("FALSE_NO_DATA_DENIAL", {x["type"] for x in v})

    def test_no_data_denial_without_data_is_honest(self):
        ctx = {"mode_anchor": {"mode": "paper"}}  # no live sections resolved
        answer = "I don't have real-time access to current market data."
        self.assertEqual(check_grounding(answer, ctx), [])


class TestExecutionAuthority(unittest.TestCase):
    def test_claims_order_authority_is_violation(self):
        answer = "I'll place the order for you now."
        v = check_grounding(answer, _healthy_zero_context())
        self.assertIn("EXECUTION_AUTHORITY_CLAIM", {x["type"] for x in v})

    def test_observation_only_statement_passes(self):
        answer = "No live order was sent. The bot is idle and I cannot place orders."
        self.assertEqual(check_grounding(answer, _healthy_zero_context()), [])


class TestDeterministicFallback(unittest.TestCase):
    def test_fallback_contains_only_verified_numbers(self):
        ctx = _healthy_zero_context()
        violations = check_grounding("The bot is executing trades and booked ₹9,999!", ctx)
        fallback = deterministic_fallback("any question", ctx, violations)
        self.assertIn("0 trade(s)", fallback)
        self.assertIn("₹0.00", fallback)
        self.assertIn("none", fallback)
        self.assertNotIn("9,999", fallback)
        self.assertNotIn("executing trades", fallback)


class TestFreshnessFromSourceTimestamps(unittest.TestCase):
    def test_stale_analysis_flagged(self):
        ctx = {"analysis": {"available": True, "data_status": "STALE", "data_age_seconds": 1200,
                            "candle_timestamp": "2026-09-26T09:15:00+05:30"}}
        ok, reason = validate_context_freshness(ctx)
        self.assertFalse(ok)
        self.assertIn("STALE", reason)

    def test_fresh_source_timestamp_ok(self):
        ctx = {"analysis": {"available": True, "data_status": "LIVE", "data_age_seconds": 12,
                            "candle_timestamp": "2026-09-26T10:00:00+05:30"}}
        ok, _ = validate_context_freshness(ctx)
        self.assertTrue(ok)

    def test_unparseable_source_timestamp_fails_closed(self):
        ctx = {"market_status": {"available": True, "timestamp": "not-a-date"}}
        ok, reason = validate_context_freshness(ctx)
        self.assertFalse(ok)
        self.assertIn("unparseable", reason)


class TestChatJobDiscardPath(unittest.TestCase):
    def _patched_settings(self):
        """Stub the settings load so the worker treats the fake adapter as a
        configured provider (the guard behavior under test is independent of
        real provider configuration)."""
        from unittest.mock import patch
        from backend.copilot.config import CopilotSettings
        settings = CopilotSettings(
            enabled=True, mode="paper", min_risk_reward=1.5,
            max_quote_age_seconds=30.0, llm_backend="openai",
            llm_base_url="http://localhost:9", llm_model="test-model",
            llm_timeout_seconds=5.0, ai_api_key="test-key-not-real",
        )
        return patch("backend.copilot.llm_adapter.load_copilot_settings", return_value=settings)

    def test_lying_adapter_answer_is_discarded(self):
        """Full job-manager path: an adapter that contradicts the context has
        its answer REPLACED by the deterministic fallback before completion."""
        from backend.copilot.chat_jobs import ChatJobManager, STATUS_COMPLETED
        from backend.copilot.llm_adapter import LLMAdapter

        class LyingAdapter(LLMAdapter):
            def explain(self, question, context, history=None):
                return "The bot is currently executing trades and has booked ₹50,000 profit."

        mgr = ChatJobManager()
        # inject the lying adapter BEFORE submitting — submit() starts the
        # worker thread immediately, so the resolver must already be patched.
        from unittest.mock import patch
        with patch("backend.copilot.chat_jobs._adapter_resolver", return_value=lambda: LyingAdapter()):
            job = mgr.submit(
                "how is the bot doing?",
                _healthy_zero_context(),
                history=[],
                state=None,
                provider_configured=True,
            )
            job.thread.join(timeout=5)
        self.assertEqual(job.status, STATUS_COMPLETED)
        self.assertNotIn("executing trades", job.answer)
        self.assertNotIn("50,000", job.answer)
        self.assertIn("0 trade(s)", job.answer)

    def test_grounded_adapter_answer_passes_through(self):
        from backend.copilot.chat_jobs import ChatJobManager, STATUS_COMPLETED
        from backend.copilot.llm_adapter import LLMAdapter

        class HonestAdapter(LLMAdapter):
            def explain(self, question, context, history=None):
                return "The bot is idle: 0 trades today, no open positions, realized P&L ₹0.00."

        mgr = ChatJobManager()
        from unittest.mock import patch
        with patch("backend.copilot.chat_jobs._adapter_resolver", return_value=lambda: HonestAdapter()):
            job = mgr.submit("status?", _healthy_zero_context(), history=[], state=None,
                             provider_configured=True)
            job.thread.join(timeout=5)
        self.assertEqual(job.status, STATUS_COMPLETED)
        self.assertIn("idle", job.answer)


class TestCopilotStrategyIdentity(unittest.TestCase):
    def test_trade_plan_payload_carries_strategy_identity(self):
        """The Copilot decision payload must state the configured V8-D
        strategy — never OPTION_PREMIUM — when the engine exposes the
        configured-strategy evaluator."""
        from backend.copilot import decision_engine

        class FakeEngine:
            strategy_name = "V8_D_PULLBACK_ATM"
            def evaluate_configured_strategy(self, symbol):
                from backend.strategy.signal import StrategySignal
                sig = StrategySignal(strategy_name="V8_D_PULLBACK_ATM", symbol=symbol)
                sig.signal = "NONE"
                sig.entry_reason = "NO TRADE"
                return sig

        class FakeTools:
            engine = FakeEngine()
            def get_live_candles(self, symbol):
                return {"available": False, "reason": "offline test"}

        out = decision_engine.build_trade_plan_for_symbol_v8d(FakeTools(), "NIFTY50")
        self.assertTrue(out.get("available"))
        self.assertEqual(out.get("strategy"), "V8_D_PULLBACK_ATM")
        self.assertIn("V8_D_PULLBACK_ATM", out.get("strategy_confirmation", ""))


if __name__ == "__main__":
    unittest.main()
