"""End-to-end proof that the AI Trading Decision layer is part of the REAL
paper execution path, and that the dashboard/Copilot tell the truth about it.

Chain under test (real PaperTradingRuntime → ExecutionPipeline → PaperBroker →
SQLite → worker persistence → scan_state.build_pipeline → API / Copilot):

    V8-D signal → AI decision → risk/sizing/kill switch → paper execution → DB → UI

The strategy stand-in is the same deterministic BUY used by the other scanner
tests (it implements the exact `evaluate_v8d_signal` interface); the AI engine
is the REAL `AITradingDecisionEngine` with a scripted provider (no network).
Nothing here fabricates market data for production paths or enables test signals.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from datetime import datetime, timezone
from unittest import mock

import pytest

from backend.ai_decision.decision_engine import AITradingDecisionEngine
from backend.copilot.provider_errors import (
    AIModelUnavailableError, AIProviderTimeoutError, AIProviderUnavailableError,
)
from backend.paper import scan_state as ss
from backend.paper.market_scan_loop import PaperMarketScanner
from backend.strategy.strategies.v8d_strategy import V8DStrategy
from backend.strategy.trading_engine import BotState
from backend.tests.test_ai_decision_layer import (
    APPROVE_JSON, REJECT_JSON, WAIT_JSON, ScriptedProvider,
)
# real fixtures from the scanner/runtime suite (worker = real PaperWorker + PaperTradingRuntime)
from backend.tests.test_scanner_runtime_pipeline import (  # noqa: F401  (worker is a fixture)
    SATURDAY, TRADING_NOW, BuyStrategy, FakeData, FixedNowScanner, _fresh_hb, _scan, worker,
)


def _engine(worker, outputs, *, enabled=True):
    eng = AITradingDecisionEngine(db=worker.db, provider=ScriptedProvider(outputs))
    eng.settings["enabled"] = enabled
    return eng


def _scanner(engine, strategy=None, now_cls=FixedNowScanner, **kw):
    return now_cls(data=FakeData(), strategy=strategy or BuyStrategy(), min_bars=60,
                   ai_engine=engine, ai_decision_pipeline_strategy="V8_D_PULLBACK_ATM", **kw)


def _pipeline(worker):
    _fresh_hb(worker.db, pid=os.getpid())
    st = ss.compute_runtime_state(worker.db, pid_alive=lambda p: True)
    return st["pipeline"], st


# ── 1. APPROVE: the full chain reaches a paper fill and is auditable ────────
def test_v8d_buy_then_ai_approve_then_risk_then_paper_fill_then_db_then_ui(worker):
    eng = _engine(worker, [APPROVE_JSON])
    rec = _scan(worker, _scanner(eng))
    assert rec["traded"] is True, rec
    assert rec["ai"]["status"] == "APPROVED" and rec["ai"]["enabled"] is True
    assert rec["ai"]["confidence"] == 82 and rec["ai"]["latency_ms"] is not None
    assert rec["risk_decision"] == "PASSED" and rec["execution_decision"] == "FILLED_PAPER"
    # the provider was really consulted, once, with the V8-D signal + context
    assert len(eng.provider.calls) == 1
    snap = json.dumps(eng.provider.calls[0]["snapshot"], default=str)
    assert "V8_D_PULLBACK_ATM" in snap and "NSE_FO|SCAN_CE_TEST" in snap
    # paper execution + DB
    assert len(worker.db.list_trades()) == 1 and len(worker.db.get_open_positions()) == 1
    # the durable AI decision and the trade share ONE signal id (auditable from the trade row)
    sid = rec["details"]["signal_id"]
    stored = eng.store.get_decisions_for_signal(sid)
    assert stored and stored[0]["decision"] == "APPROVE"
    assert worker.db.list_trades()[0].get("signal_id", sid) == sid
    # UI view
    pipe, _ = _pipeline(worker)
    assert pipe["latest_signal"] == "BUY CE"
    assert pipe["ai_decision"] == "APPROVED" and pipe["risk_check"] == "PASS"
    assert pipe["execution"] == "FILLED (PAPER)" and pipe["market"] == "LIVE"
    assert pipe["strategy"] == "V8_D_PULLBACK_ATM"


# ── 2. AI REJECT / WAIT / failures: NO order, clear reason ──────────────────
@pytest.mark.parametrize("outputs, status, label", [
    ([REJECT_JSON], "REJECTED", "REJECTED"),
    ([WAIT_JSON], "REJECTED", "REJECTED"),      # model said WAIT inside the contract -> still no trade
])
def test_ai_reject_and_wait_block_the_order(worker, outputs, status, label):
    eng = _engine(worker, outputs)
    rec = _scan(worker, _scanner(eng))
    assert rec["traded"] is False and rec["signal"] == "BUY"
    assert rec["reason"].startswith("AI_NO_TRADE:"), rec["reason"]
    assert rec["ai"]["status"] in ("REJECTED", "WAIT")
    assert not worker.db.list_trades() and not worker.db.get_open_positions()
    pipe, _ = _pipeline(worker)
    assert pipe["latest_signal"] == "BUY CE"
    assert pipe["ai_decision"].startswith(("REJECTED", "WAIT"))
    assert pipe["ai_reason"]                                   # the actual reason is shown
    assert pipe["risk_check"] == "NOT EVALUATED" and pipe["execution"] == "NOT ATTEMPTED"
    assert "AI" in pipe["summary"] and "No trade" in pipe["summary"]


@pytest.mark.parametrize("failure", [
    AIProviderUnavailableError("connection refused"),
    AIProviderTimeoutError("timed out"),
    AIModelUnavailableError("model not loaded"),
    "this is prose, not JSON",
    '{"decision": "MAYBE"}',
])
def test_ai_failure_fails_safe_and_is_reported_as_unavailable_not_as_a_verdict(worker, failure):
    eng = _engine(worker, [failure])
    rec = _scan(worker, _scanner(eng))
    assert rec["traded"] is False and rec["reason"].startswith("AI_NO_TRADE:")
    assert rec["ai"]["status"] == "UNAVAILABLE", rec["ai"]
    assert not worker.db.list_trades() and not worker.db.get_open_positions()
    pipe, st = _pipeline(worker)
    assert pipe["ai_decision"] == "UNAVAILABLE — FAILED SAFE (NO TRADE)"
    assert pipe["execution"] == "NOT ATTEMPTED"


def test_slow_ai_is_bounded_then_replays_without_a_second_inference(worker, monkeypatch):
    monkeypatch.setenv("AI_DECISION_BUDGET_SECONDS", "0.2")
    release = threading.Event()

    class Slow(ScriptedProvider):
        def chat_json(self, snapshot):
            self.calls.append({"snapshot": snapshot})
            release.wait(timeout=10)
            return APPROVE_JSON

    prov = Slow([])
    eng = AITradingDecisionEngine(db=worker.db, provider=prov)
    eng.settings["enabled"] = True
    t0 = time.monotonic()
    rec = _scan(worker, _scanner(eng))
    assert time.monotonic() - t0 < 5, "scan must not wait for a slow model"
    assert rec["traded"] is False and "AI_WAITING" in rec["reason"]
    assert rec["ai"]["status"] == "WAIT"
    release.set()                                                # model finishes in the background
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not eng.store.get_latest_setup_decision(
            __import__("backend.ai_decision.setup_identity", fromlist=["x"]).build_setup_id(
                signal=BuyStrategy().evaluate_v8d_signal(underlying_symbol="NIFTY50", spot_price=24050.0)[0],
                contract=BuyStrategy().evaluate_v8d_signal(underlying_symbol="NIFTY50", spot_price=24050.0)[0].indicators["selected_contract"],
                expiry="2027-01-07", candles=FakeData().candles)):
        time.sleep(0.05)
    rec2 = _scan(worker, _scanner(eng))
    assert rec2["traded"] is True, rec2["reason"]                # later scan: stored verdict replayed
    assert len(prov.calls) == 1                                  # ONE inference total


# ── 3. AI can never bypass risk / kill switch / sizing ──────────────────────
def test_kill_switch_stops_before_the_ai_is_even_consulted(worker):
    from backend.execution.kill_switch import FULL_SYSTEM_STOP
    worker.runtime.kill.set_level(FULL_SYSTEM_STOP, "test")
    eng = _engine(worker, [APPROVE_JSON])
    rec = _scan(worker, _scanner(eng))
    assert rec["traded"] is False and rec["reason"].startswith("rejected:kill_switch")
    assert eng.provider.calls == []                              # no inference burned
    assert rec["ai"]["status"] == "NOT_EVALUATED" and "Kill switch" in rec["ai"]["reason"]
    assert not worker.db.list_trades()
    pipe, _ = _pipeline(worker)
    assert pipe["ai_decision"] == "NOT EVALUATED" and pipe["risk_check"] == "REJECTED"


def test_ai_approve_cannot_override_persistent_kill_switch_or_risk(worker):
    class FakeKill:                                   # scanner-level switch reads OFF …
        def level(self): return "OFF"
        def blocks_entries(self): return False
        def requires_flatten(self): return False
    worker.runtime.kill = FakeKill()
    worker.db.save_setting("persistent_kill_level", "STOP_NEW_ENTRIES")   # … but the pipeline gate is ON
    worker.db.save_setting("persistent_kill_level_reason", "test")
    eng = _engine(worker, [APPROVE_JSON])
    rec = _scan(worker, _scanner(eng))
    assert rec["ai"]["status"] == "APPROVED"                    # the AI said yes …
    assert rec["traded"] is False and "kill" in rec["reason"].lower()   # … hard risk said no
    assert not worker.db.list_trades() and not worker.db.get_open_positions()
    pipe, _ = _pipeline(worker)
    assert pipe["ai_decision"] == "APPROVED" and pipe["risk_check"] == "REJECTED"
    assert pipe["execution"] == "REJECTED"


def test_ai_cannot_change_price_stop_target_or_quantity(worker):
    hostile = json.dumps({"decision": "APPROVE", "confidence": 99, "reason_codes": ["TREND_PASS"],
                          "entry_price": 1.0, "stop_loss": 1.0, "target": 1e9, "quantity": 999999,
                          "lot_size": 1, "instrument_key": "NSE_FO|EVIL"})
    eng = _engine(worker, [hostile])
    rec = _scan(worker, _scanner(eng))
    assert rec["traded"] is True
    t = worker.db.list_trades()[0]
    assert int(t["quantity"]) == 75                              # V8-D / sizer quantity, not the AI's
    assert "EVIL" not in json.dumps(t, default=str)
    pos = worker.db.get_open_positions()[0]
    assert pos.instrument_key == "NSE_FO|SCAN_CE_TEST" and int(pos.quantity) == 75


def test_daily_trade_limit_still_binds_with_ai_approving_everything(worker):
    eng = _engine(worker, [APPROVE_JSON] * 5)
    strat = V8DStrategy()
    cap = strat.max_daily_trades
    assert cap >= 1
    rec = _scan(worker, _scanner(eng, strategy=strat))           # real V8-D on flat candles: NO SIGNAL
    assert rec["traded"] is False and eng.provider.calls == []   # AI is not consulted without a BUY
    assert rec["ai"]["status"] == "NOT_EVALUATED"


def test_duplicate_signal_does_not_reconsult_ai_or_open_a_second_position(worker):
    eng = _engine(worker, [APPROVE_JSON, APPROVE_JSON])
    sc = _scanner(eng)
    rec1 = _scan(worker, sc)
    assert rec1["traded"] is True and len(eng.provider.calls) == 1
    rec2 = _scan(worker, sc)                                      # same underlying, position still open
    assert rec2["traded"] is False and rec2["reason"].startswith("POSITION_ALREADY_OPEN")
    assert len(eng.provider.calls) == 1                           # guard sits BEFORE the AI: no 2nd inference
    assert len(worker.db.list_trades()) == 1 and len(worker.db.get_open_positions()) == 1
    pipe, _ = _pipeline(worker)
    assert pipe["execution"] == "NOT ATTEMPTED" and pipe["latest_signal"] == "BUY CE"


def test_stale_market_data_never_reaches_the_ai_or_the_broker(worker):
    from datetime import timedelta
    from backend.tests.test_scanner_runtime_pipeline import _candles
    eng = _engine(worker, [APPROVE_JSON])
    sc = FixedNowScanner(data=FakeData(candles=_candles(now=TRADING_NOW - timedelta(hours=5))),
                         strategy=BuyStrategy(), min_bars=60, max_candle_age_seconds=600,
                         ai_engine=eng, ai_decision_pipeline_strategy="V8_D_PULLBACK_ATM")
    rec = _scan(worker, sc)
    assert rec["traded"] is False and rec["reason"].startswith("stale_candles")
    assert eng.provider.calls == [] and not worker.db.list_trades()
    pipe, _ = _pipeline(worker)
    assert pipe["latest_signal"] == "NOT EVALUATED — DATA PROBLEM"


# ── 4. Honest "AI not consulted" states ─────────────────────────────────────
def test_ai_disabled_is_reported_as_disabled_and_never_pretends(worker):
    eng = _engine(worker, [APPROVE_JSON], enabled=False)
    rec = _scan(worker, _scanner(eng))
    assert rec["traded"] is True                                  # V8-D + risk controls only
    assert eng.provider.calls == []
    assert rec["ai"]["status"] == "DISABLED" and rec["ai"]["enabled"] is False
    pipe, _ = _pipeline(worker)
    assert pipe["ai_decision"] == "DISABLED" and "disabled" in pipe["ai_reason"].lower()
    assert pipe["execution"] == "FILLED (PAPER)"


def test_no_ai_engine_attached_is_reported_as_disabled(worker):
    rec = _scan(worker, _scanner(None))
    assert rec["ai"]["status"] == "DISABLED"


def test_operator_toggle_overrides_env_each_scan(worker):
    eng = _engine(worker, [REJECT_JSON], enabled=True)
    worker.db.save_setting("ai_decision_enabled_override", "0")          # operator switches AI OFF
    rec = _scan(worker, _scanner(eng))
    assert rec["ai"]["status"] == "DISABLED" and rec["traded"] is True
    assert eng.provider.calls == []
    worker.runtime.broker.positions.clear()
    worker.db.save_setting("ai_decision_enabled_override", "1")          # … and back ON (next scan)
    rec2 = _scan(worker, _scanner(eng, strategy=BuyStrategy()))
    assert rec2["ai"]["status"] in ("REJECTED", "WAIT", "UNAVAILABLE", "APPROVED")
    assert len(eng.provider.calls) >= 1


def test_no_signal_shows_the_actual_reason_and_ai_not_evaluated(worker):
    eng = _engine(worker, [APPROVE_JSON])
    rec = _scan(worker, _scanner(eng, strategy=V8DStrategy()))            # REAL V8-D, flat candles
    assert rec["traded"] is False and rec["reason"].startswith("no_trade:")
    pipe, _ = _pipeline(worker)
    assert pipe["latest_signal"] == "NO SIGNAL" and pipe["signal_detail"]
    assert pipe["ai_decision"] == "NOT EVALUATED" and "no V8-D BUY" in pipe["ai_reason"]
    assert pipe["risk_check"] == "NOT EVALUATED" and pipe["execution"] == "NOT ATTEMPTED"
    assert eng.provider.calls == []


def test_market_closed_is_shown_as_market_closed_not_as_a_strategy_outcome(worker):
    class Sat(FixedNowScanner):
        fixed_now = SATURDAY
    eng = _engine(worker, [APPROVE_JSON])
    rec = _scan(worker, _scanner(eng, now_cls=Sat))
    assert rec["reason"] == "market_closed"
    pipe, st = _pipeline(worker)
    assert pipe["market"] == "MARKET CLOSED"
    assert pipe["latest_signal"] == "NOT EVALUATED — MARKET CLOSED"
    assert pipe["ai_decision"] == "NOT EVALUATED" and pipe["execution"] == "NOT ATTEMPTED"
    assert st["state"] == ss.RUNNING_WAITING_FOR_MARKET
    assert eng.provider.calls == []


def test_data_failure_is_shown_as_data_problem(worker):
    eng = _engine(worker, [APPROVE_JSON])
    sc = FixedNowScanner(data=FakeData(candle_exc=RuntimeError("401")), strategy=BuyStrategy(),
                         min_bars=60, ai_engine=eng, ai_decision_pipeline_strategy="V8_D_PULLBACK_ATM")
    _scan(worker, sc)
    pipe, _ = _pipeline(worker)
    assert pipe["latest_signal"] == "NOT EVALUATED — DATA PROBLEM"
    assert pipe["scanner"] == "RUNNING — DATA ERROR" and eng.provider.calls == []


def test_pipeline_before_any_scan_is_honest(worker):
    pipe, _ = _pipeline(worker)
    assert pipe["latest_signal"] == "NO SCAN YET" and pipe["execution"] == "NOT ATTEMPTED"
    assert pipe["ai_decision"] in ("DISABLED", "NOT EVALUATED")


# ── 5. API + Copilot read the same truth ────────────────────────────────────
def test_bot_status_api_and_copilot_context_expose_the_pipeline(worker):
    from backend.api.routers import bot_control
    from backend.copilot import full_context
    eng = _engine(worker, [REJECT_JSON])
    _scan(worker, _scanner(eng))
    _fresh_hb(worker.db, pid=os.getpid())
    out = asyncio.run(bot_control.bot_status())
    p = out["runtime"]["pipeline"]
    assert p["ai_decision"].startswith("REJECTED") and p["latest_signal"] == "BUY CE"
    ctx_p = full_context._pipeline(worker.db)
    assert ctx_p["ai_decision"] == p["ai_decision"] and ctx_p["source"] == "SCANNER"


def test_copilot_answers_ai_architecture_deterministically_and_correctly(worker):
    from backend.copilot.ai_architecture import format_ai_architecture, is_ai_architecture_question
    from backend.copilot.conversational import route_question
    from backend.copilot.config import CopilotSettings
    from backend.copilot.llm_adapter import LocalOpenAICompatibleAdapter, RuleBasedFallbackAdapter

    q = "So there is another AI in my bot for decision making?"
    assert is_ai_architecture_question(q)
    assert route_question(q).__name__ == "_plan_ai_architecture"
    for ok in ("Explain the latest rejection", "Why didn't the bot trade?", "Explain the latest backtest"):
        assert not is_ai_architecture_question(ok)

    ctx_on = {"_intent": "GENERAL", "_ai_architecture": True,
              "ai": {"available": True, "enabled": True, "provider": "ollama", "model": "llama3.2:1b",
                     "latest": {"available": True, "decision": "REJECT", "confidence": 71,
                                "reason_codes": ["POOR_RISK_REWARD"]}},
              "pipeline": {"latest_signal": "BUY CE", "ai_decision": "REJECTED",
                           "risk_check": "NOT EVALUATED", "execution": "NO TRADE"}}
    a = format_ai_architecture(ctx_on)
    assert "two separate AI components" in a and "ENABLED" in a and "llama3.2:1b" in a
    assert "I am not the one making trades" in a and "sole AI" not in a
    assert "POOR_RISK_REWARD" in a and "REJECTED" in a
    off = format_ai_architecture({"ai": {"available": True, "enabled": False}})
    assert "DISABLED" in off and "two separate AI components" in off
    unknown = format_ai_architecture({"ai": {"available": False, "reason": "db down"}})
    assert "can't confirm" in unknown

    # the LLM adapter must NOT ask the model for this — it answers from state
    adapter = LocalOpenAICompatibleAdapter(CopilotSettings.__new__(CopilotSettings))
    with mock.patch.object(adapter, "_send_chat", side_effect=AssertionError("model must not be called")):
        assert "two separate AI components" in adapter.explain(q, ctx_on)
    assert "two separate AI components" in RuleBasedFallbackAdapter().explain(q, ctx_on)


def test_copilot_no_longer_shows_stale_worker_error_on_a_healthy_worker(worker):
    from backend.copilot import full_context
    worker.db.save_setting(ss.ERR_KEY, "Paper worker already running (pid=41770)")
    _scan(worker, _scanner(_engine(worker, [APPROVE_JSON])))
    _fresh_hb(worker.db, pid=os.getpid())
    sc = full_context._scanner(None, worker.db)
    assert not sc.get("error"), sc                               # healthy state: no phantom error
    assert worker.db.get_setting(ss.ERR_KEY, "") == ""            # cleared by the clean scan


# ── 6. Duplicate worker must not clobber the healthy worker's state ─────────
def test_second_worker_losing_the_lock_changes_nothing(worker):
    """A duplicate worker (a DIFFERENT live process holds the lock) must exit
    without touching the healthy worker's pid/heartbeat/status/error rows."""
    import subprocess
    from backend.paper.paper_worker import PaperWorker
    from backend.paper.worker_lock import WorkerLockError
    holder = subprocess.Popen(["sleep", "60"])            # a real, live, foreign PID
    try:
        worker.lock.lock_path.write_text(str(holder.pid))
        worker._write_hb("running")
        before = {k: worker.db.get_setting(k, "") for k in
                  (ss.HB_KEY, ss.PID_KEY, ss.STATUS_KEY, ss.ERR_KEY, ss.LOOP_KEY)}
        second = PaperWorker()
        with pytest.raises(WorkerLockError, match="already running"):
            second.start()
        after = {k: worker.db.get_setting(k, "") for k in before}
        assert after == before, (before, after)
        assert after[ss.STATUS_KEY] == "running" and after[ss.ERR_KEY] == ""
        assert after[ss.PID_KEY] == str(os.getpid())      # NOT the loser's pid
        second._hb_stop.set()
    finally:
        holder.kill()
        holder.wait()


# ── 7. Static safety guarantees that keep AI subordinate ────────────────────
def test_ai_is_wired_between_v8d_and_submit_entry_in_the_paper_scan():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "backend/paper/market_scan_loop.py").read_text(encoding="utf-8")
    i_v8d = src.index("self.strategy.evaluate_v8d_signal(")
    i_gate = src.index("apply_ai_decision_gate(\n                payload")
    i_submit = src.index("runtime.submit_entry(payload)")
    assert i_v8d < i_gate < i_submit, "AI gate must sit between V8-D and execution"


def test_ai_decision_module_cannot_reach_orders():
    """AST-level (docstrings/comments don't count): the AI package neither
    imports nor references any order/broker/execution symbol."""
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "backend/ai_decision"
    banned = {"place_order", "PaperBroker", "LiveBroker", "OrderManager", "ExecutionPipeline",
              "submit_entry", "UpstoxClient", "submit_signal", "IdempotentOrderStore"}
    banned_modules = ("backend.broker", "backend.execution", "backend.orders", "backend.paper.paper_broker",
                      "backend.paper.paper_runtime")
    for f in root.glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(banned_modules), f"{f.name} imports {node.module}"
                names += [a.name for a in node.names]
            elif isinstance(node, ast.Import):
                for a in node.names:
                    assert not a.name.startswith(banned_modules), f"{f.name} imports {a.name}"
            for n in names:
                assert n not in banned, f"{f.name} references {n}"
