"""Regression tests for the restored Copilot.

Covers (per the Issue-2 requirements):
- route/nav/API registration (the tab was deleted in commit b35dd60)
- async chat job flow: submit returns a job id immediately, polling is
  short, cancellation works
- distinct typed provider errors: unavailable / auth / rate limit /
  model unavailable / timeout / backend exception — never collapsed
  into a generic timeout
- no-provider honesty: PROVIDER_NOT_CONFIGURED, never a canned fake answer
- context grounding: BotContext / TradeContext / BacktestContext built
  from real tools, question-relevant only
- secrets never included in context or answers
- conversation history recorded only for completed answers
"""
from __future__ import annotations

import threading
import time
import unittest
import uuid
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.copilot.config import CopilotSettings
from backend.copilot.conversation_state import clear_session, get_session
from backend.copilot.secret_guard import collect_secret_values, redact_value
from backend.copilot.provider_errors import (
    AIProviderAuthError,
    AIProviderError,
    AIProviderRateLimitError,
    AIProviderTimeoutError,
    AIProviderUnavailableError,
    AIModelUnavailableError,
)


def _settings(**over):
    base = dict(
        enabled=True, mode="shadow", min_risk_reward=1.5,
        max_quote_age_seconds=30, llm_backend="local_openai_compatible",
        llm_base_url="http://localhost:11434/v1", llm_model="llama3.1:8b",
        llm_timeout_seconds=2,
    )
    base.update(over)
    return CopilotSettings(**base)


def _app():
    from backend.api.routers.copilot import router

    app = FastAPI()
    app.include_router(router, prefix="/api/copilot")
    app.state.engine = None
    app.state.health_monitor = None
    app.state.scanner = None
    app.state.ws_client = None
    return app


def _stub_tools(app):
    """Attach a stub CopilotTools so context resolution is deterministic."""

    class _StubTools:
        def get_market_status(self):
            return {"available": True, "market_open": True, "websocket_connected": True}

        def get_bot_health(self):
            return {"available": True, "status": "RUNNING"}

        def get_account_risk(self):
            return {"available": True, "exposure_pct": 0.0}

        def get_open_positions(self):
            return {"available": True, "positions": []}

        def get_daily_pnl(self):
            return {"available": True, "date": "2026-09-25", "trades_today": 1, "realized_pnl": 120.5}

        def get_recent_trades(self, limit=5):
            return {"available": True, "trades": [{
                "symbol": "NIFTY50", "strike": 25000, "option_type": "CE",
                "expiry": "2026-10-01", "entry_price": 120.0, "quantity": 75,
                "lot_size": 75, "net_pnl": 120.5, "status": "CLOSED",
            }]}

        def run_full_diagnostics(self):
            return {"available": True, "overall_status": "OK", "rows": []}

        def get_live_candles(self, symbol, timeframe="5minute", limit=100):
            return {"available": False, "reason": "stub"}

        def get_trade_plan(self, symbol):
            return {"available": False, "reason": "stub"}

    return _StubTools()


class TestCopilotRoutesAndNav(unittest.TestCase):
    def test_router_registers_all_chat_routes(self):
        from backend.api.routers.copilot import router

        paths = {r.path for r in router.routes}
        self.assertIn("/chat/submit", paths)
        self.assertIn("/chat/status/{job_id}", paths)
        self.assertIn("/chat/status/{job_id}/cancel", paths)
        self.assertIn("/chat", paths)
        self.assertIn("/status", paths)

    def test_copilot_router_mounted_in_main_app(self):
        from backend.api.main import app as main_app

        def _walk(routes):
            for r in routes:
                path = getattr(r, "path", None)
                if path:
                    yield path
                # FastAPI >=0.120 wraps include_router calls in
                # _IncludedRouter objects: recurse into the original
                # router, prefixing with the include prefix.
                prefix = ""
                orig = getattr(r, "original_router", None)
                ic = getattr(r, "include_context", None)
                if ic is not None:
                    prefix = getattr(ic, "prefix", "") or ""
                if orig is not None:
                    for sub_path in _walk(orig.routes):
                        yield prefix + sub_path

        all_paths = list(_walk(main_app.routes))
        copilot_paths = [p for p in all_paths if p.startswith("/api/copilot")]
        self.assertTrue(any("/chat/submit" in p for p in copilot_paths),
                        f"copilot chat submit missing from main app: {copilot_paths}")
        self.assertTrue(any(p.rstrip("/").endswith("/status") for p in copilot_paths),
                        "copilot /status endpoint missing")

    def test_nav_contract_copilot_route_and_import(self):
        """The tab vanished because App.tsx/Layout lost the route + lazy
        import — guard both, plus the nav entry, against regression."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src"
        app_tsx = (root / "App.tsx").read_text(encoding="utf-8")
        layout = (root / "components" / "Layout.tsx").read_text(encoding="utf-8")
        self.assertIn("pages/Copilot", app_tsx)
        self.assertIn('"/copilot"', app_tsx)
        self.assertIn("Copilot AI", layout)
        self.assertIn("'/copilot'", layout)

    def test_copilot_page_exists_and_uses_async_jobs(self):
        import pathlib

        page = (pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages" / "Copilot.tsx").read_text(encoding="utf-8")
        self.assertIn("/api/copilot/chat/submit", page)
        self.assertIn("/api/copilot/chat/status/", page)


class TestChatSubmitFlow(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(_app())
        clear_session("sess-test")
        from backend.copilot.chat_jobs import chat_job_manager

        chat_job_manager._jobs.clear()

    def _patch_settings(self):
        return patch("backend.api.routers.copilot.load_copilot_settings",
                     return_value=_settings())

    def test_submit_returns_job_id_immediately(self):
        with self._patch_settings(), patch(
            "backend.api.routers.copilot._build_tools",
            return_value=_stub_tools(None),
        ), patch("backend.api.routers.copilot._resolve_context",
                 return_value={"bot_health": {"available": True}}), patch(
            "backend.copilot.llm_adapter.get_llm_adapter",
        ) as adapter_mock:
            instance = MagicMock()
            instance.explain.return_value = "Grounded answer from the provider."
            adapter_mock.return_value = instance

            t0 = time.monotonic()
            r = self.client.post("/api/copilot/chat/submit",
                                 json={"question": "How is the bot?", "session_id": "sess-test"})
            elapsed = time.monotonic() - t0
        self.assertEqual(r.status_code, 202, r.text)
        body = r.json()
        self.assertTrue(body["job_id"])
        # Job may already be 'thinking' by the time we assert — the
        # contract is only that submit returned immediately with a job id
        # (never the final answer inside the POST response).
        self.assertIn(body["status"], ("queued", "thinking"))
        self.assertNotIn("answer", body)
        self.assertLess(elapsed, 5.0, "submit must not wait for the provider")

        job_id = body["job_id"]
        deadline = time.monotonic() + 10
        final = None
        while time.monotonic() < deadline:
            s = self.client.get(f"/api/copilot/chat/status/{job_id}").json()
            if s["status"] == "completed":
                final = s
                break
            time.sleep(0.05)
        self.assertIsNotNone(final, "job never completed")
        self.assertEqual(final["answer"], "Grounded answer from the provider.")

    def test_submit_without_provider_is_honest_not_canned(self):
        with patch("backend.api.routers.copilot.load_copilot_settings",
                   return_value=_settings(llm_backend="none")):
            r = self.client.post("/api/copilot/chat/submit",
                                 json={"question": "Why didn't the bot trade?"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIsNone(body["job_id"])
        self.assertEqual(body["error_code"], "PROVIDER_NOT_CONFIGURED")
        self.assertIn("No AI provider is configured", body["error"])

    def test_legacy_chat_with_provider_returns_job_envelope(self):
        with self._patch_settings(), patch(
            "backend.api.routers.copilot._build_tools",
            return_value=_stub_tools(None),
        ), patch("backend.api.routers.copilot._resolve_context",
                 return_value={"bot_health": {"available": True}}), patch(
            "backend.copilot.llm_adapter.get_llm_adapter",
        ) as adapter_mock:
            instance = MagicMock()
            instance.explain.return_value = "ok"
            adapter_mock.return_value = instance
            r = self.client.post("/api/copilot/chat",
                                 json={"question": "hi", "session_id": "sess-legacy"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body.get("adapter"), "async_job")
        self.assertTrue(body.get("job_id"))

    def test_legacy_chat_without_provider_never_returns_canned_answer(self):
        with patch("backend.api.routers.copilot.load_copilot_settings",
                   return_value=_settings(llm_backend="none")):
            r = self.client.post("/api/copilot/chat",
                                 json={"question": "What is the current premium?", "session_id": "sess-x"})
        body = r.json()
        self.assertEqual(body.get("error_code"), "PROVIDER_NOT_CONFIGURED")
        self.assertIn("No AI provider is configured", body["answer"])

    def test_cancel_running_job(self):
        release = threading.Event()

        class _BlockingAdapter:
            def explain(self, *a, **k):
                release.wait(timeout=10)
                return "late"

        with self._patch_settings(), patch(
            "backend.api.routers.copilot._build_tools",
            return_value=_stub_tools(None),
        ), patch("backend.api.routers.copilot._resolve_context",
                 return_value={}), patch(
            "backend.copilot.llm_adapter.get_llm_adapter",
            return_value=_BlockingAdapter(),
        ):
            r = self.client.post("/api/copilot/chat/submit",
                                 json={"question": "long question", "session_id": "sess-cancel"})
            job_id = r.json()["job_id"]
            time.sleep(0.2)  # let the worker enter explain()
            c = self.client.post(f"/api/copilot/chat/status/{job_id}/cancel")
            release.set()

        self.assertEqual(c.status_code, 200)
        deadline = time.monotonic() + 10
        status = None
        while time.monotonic() < deadline:
            status = self.client.get(f"/api/copilot/chat/status/{job_id}").json()
            if status["status"] in ("cancelled", "completed", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(status["status"], "cancelled")


class TestProviderErrors(unittest.TestCase):
    """Each provider failure mode must map to a DISTINCT typed code."""

    def setUp(self):
        self.client = TestClient(_app())
        from backend.copilot.chat_jobs import chat_job_manager

        chat_job_manager._jobs.clear()

    def _submit_and_wait_failed(self, exc):
        with patch("backend.api.routers.copilot.load_copilot_settings",
                   return_value=_settings()), patch(
            "backend.api.routers.copilot._build_tools",
            return_value=_stub_tools(None),
        ), patch("backend.api.routers.copilot._resolve_context",
                 return_value={"bot_health": {"available": True}}), patch(
            "backend.copilot.llm_adapter.get_llm_adapter",
        ) as adapter_mock:
            instance = MagicMock()
            instance.explain.side_effect = exc
            adapter_mock.return_value = instance
            r = self.client.post("/api/copilot/chat/submit",
                                 json={"question": "status?", "session_id": None})
            job_id = r.json()["job_id"]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                s = self.client.get(f"/api/copilot/chat/status/{job_id}").json()
                if s["status"] == "failed":
                    return s
                time.sleep(0.05)
        raise AssertionError("job did not fail")

    def test_provider_unavailable(self):
        s = self._submit_and_wait_failed(AIProviderUnavailableError("connection refused"))
        self.assertEqual(s["error_code"], "PROVIDER_UNAVAILABLE")

    def test_provider_timeout(self):
        s = self._submit_and_wait_failed(AIProviderTimeoutError("read timed out"))
        self.assertEqual(s["error_code"], "PROVIDER_TIMEOUT")

    def test_provider_auth_failure(self):
        s = self._submit_and_wait_failed(AIProviderAuthError("401"))
        self.assertEqual(s["error_code"], "PROVIDER_AUTH_FAILED")

    def test_provider_rate_limit(self):
        s = self._submit_and_wait_failed(AIProviderRateLimitError("429"))
        self.assertEqual(s["error_code"], "PROVIDER_RATE_LIMITED")

    def test_model_unavailable(self):
        s = self._submit_and_wait_failed(AIModelUnavailableError("model not found"))
        self.assertEqual(s["error_code"], "MODEL_UNAVAILABLE")

    def test_backend_exception(self):
        s = self._submit_and_wait_failed(ValueError("boom"))
        self.assertEqual(s["error_code"], "BACKEND_EXCEPTION")

    def test_typed_error_codes_are_all_distinct(self):
        codes = {
            AIProviderUnavailableError().code,
            AIProviderTimeoutError().code,
            AIProviderAuthError().code,
            AIProviderRateLimitError().code,
            AIModelUnavailableError().code,
            AIProviderError().code,
        }
        self.assertEqual(len(codes), 6)


class TestContextGrounding(unittest.TestCase):
    def _tools(self):
        return _stub_tools(None)

    def test_trade_question_pulls_trade_context(self):
        from backend.copilot.context import build_context

        ctx = build_context("Show today's trades", self._tools())
        self.assertIn("recent_trades", ctx)
        t = ctx["recent_trades"]["trades"][0]
        for field in ("symbol", "strike", "option_type", "expiry", "entry_price", "quantity", "net_pnl"):
            self.assertIn(field, t)

    def test_health_question_pulls_bot_context(self):
        from backend.copilot.context import build_context

        ctx = build_context("Is the paper worker running?", self._tools())
        self.assertIn("bot_health", ctx)
        self.assertIn("market_status", ctx)

    def test_generic_question_gets_minimal_context(self):
        from backend.copilot.context import build_context

        ctx = build_context("What is VWAP?", self._tools())
        self.assertNotIn("recent_trades", ctx)
        self.assertNotIn("bot_health", ctx)

    def test_backtest_context_reads_latest_job(self):
        from backend.backtest.task_manager import task_manager
        from backend.copilot.context import build_context

        task = task_manager.create_task(symbols=["NIFTY50"],
                                        start_date="2026-07-01",
                                        end_date="2026-09-25",
                                        interval="5minute")
        try:
            task_manager.update_progress(task.task_id, {"phase": "processing"},
                                         status="RUNNING")
            ctx = build_context("Explain the latest backtest", self._tools())
            self.assertIn("backtest", ctx)
            self.assertEqual(ctx["backtest"]["status"], "RUNNING")
            self.assertEqual(ctx["backtest"]["interval"], "5minute")
        finally:
            task_manager._tasks.pop(task.task_id, None)


class TestSecretsNeverLeak(unittest.TestCase):
    SECRET = "upstox-token-value-1234567890"

    def setUp(self):
        self.patches = [
            patch.dict("os.environ", {"UPSTOX_ACCESS_TOKEN": self.SECRET}),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_secret_values_redacted_from_context(self):
        from backend.copilot.context import build_context

        secret = self.SECRET

        class LeakyTools:
            def get_market_status(self):
                return {"available": True, "note": f"token {secret}"}

            def get_bot_health(self):
                return {"available": True, "access_token": secret}

            def get_account_risk(self):
                return {"available": True}

            def get_open_positions(self):
                return {"available": True}

            def get_daily_pnl(self):
                return {"available": True}

            def get_recent_trades(self, limit=5):
                return {"available": True, "trades": []}

        ctx = build_context("Is the paper worker running?", LeakyTools())
        s = str(ctx)
        self.assertNotIn(self.SECRET, s)
        self.assertIn("[REDACTED]", s)

    def test_sensitive_keys_dropped(self):
        data = {"access_token": self.SECRET, "client_secret": "cs",
                "safe": "value"}
        out = redact_value(data, collect_secret_values([self.SECRET]))
        self.assertNotIn("access_token", out)
        self.assertNotIn("client_secret", out)
        self.assertEqual(out["safe"], "value")

    def test_bearer_headers_redacted_from_text(self):
        from backend.copilot.secret_guard import redact_text

        text = f"Authorization: Bearer {self.SECRET}"
        out = redact_text(text, collect_secret_values([self.SECRET]))
        self.assertNotIn(self.SECRET, out)

    def test_answer_redaction_in_completed_job(self):
        from backend.copilot.chat_jobs import chat_job_manager
        from backend.copilot.llm_adapter import LLMAdapter

        client = TestClient(_app())
        secret = self.SECRET

        class LeakyAdapter(LLMAdapter):
            def explain(self, question, context, history=None):
                return f"Your token is {secret} per the data."

        with patch("backend.api.routers.copilot.load_copilot_settings",
                   return_value=_settings()), patch(
            "backend.api.routers.copilot._build_tools",
            return_value=_stub_tools(None),
        ), patch("backend.api.routers.copilot._resolve_context",
                 return_value={}), patch(
            "backend.copilot.llm_adapter.get_llm_adapter",
            return_value=LeakyAdapter(),
        ):
            r = client.post("/api/copilot/chat/submit",
                            json={"question": "q", "session_id": "sess-leak"})
            job_id = r.json()["job_id"]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                s = client.get(f"/api/copilot/chat/status/{job_id}").json()
                if s["status"] in ("completed", "failed"):
                    break
                time.sleep(0.05)
        self.assertEqual(s["status"], "completed")
        self.assertNotIn(self.SECRET, s["answer"])
        self.assertIn("[REDACTED]", s["answer"])


class TestConversationHistory(unittest.TestCase):
    def test_history_recorded_only_for_completed_answers(self):
        session_id = f"sess-hist-{uuid.uuid4()}"
        from backend.copilot.chat_jobs import chat_job_manager

        chat_job_manager._jobs.clear()
        client = TestClient(_app())

        with patch("backend.api.routers.copilot.load_copilot_settings",
                   return_value=_settings()), patch(
            "backend.api.routers.copilot._build_tools",
            return_value=_stub_tools(None),
        ), patch("backend.api.routers.copilot._resolve_context",
                 return_value={"bot_health": {"available": True}}), patch(
            "backend.copilot.llm_adapter.get_llm_adapter",
        ) as adapter_mock:
            instance = MagicMock()
            instance.explain.return_value = "Grounded answer."
            adapter_mock.return_value = instance
            r = client.post("/api/copilot/chat/submit",
                            json={"question": "How is the bot?", "session_id": session_id})
            job_id = r.json()["job_id"]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                s = client.get(f"/api/copilot/chat/status/{job_id}").json()
                if s["status"] in ("completed", "failed"):
                    break
                time.sleep(0.05)

        self.assertEqual(s["status"], "completed")
        state = get_session(session_id)
        roles = [t.role for t in state.turns]
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)
        self.assertEqual(state.turns[-1].text, "Grounded answer.")
        clear_session(session_id)


if __name__ == "__main__":
    unittest.main()
