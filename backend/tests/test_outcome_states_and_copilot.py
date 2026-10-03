"""Distinct outcome states, Copilot honesty, quote-age wiring, single execution path,
Option Scanner indicators."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from backend.copilot.gate_chain import _stage_from_reason, build_gate_chain_from_db
from backend.copilot.why_no_trade import format_why_no_trade, is_why_no_trade_question
from backend.paper import scan_state as ss
from backend.strategy.strategies.v8d_strategy import V8DStrategy
from backend.strategy.v8d_diagnostics import explain_pullback
from backend.tests.test_ai_decision_layer import APPROVE_JSON, REJECT_JSON
from backend.tests.test_ai_pipeline_e2e import _engine, _pipeline
from backend.tests.test_scanner_runtime_pipeline import _scan, worker  # noqa: F401
from backend.tests.test_v8d_signal_to_execution import _find, _in_entry_window, _now_after_close, _scanner
from backend.tests.test_v8d_diagnostics import WINDOW, _chain_for, _load

ROOT = Path(__file__).resolve().parents[2]


# ── NO_SIGNAL vs SIGNAL_REJECTED (and the other distinct states) ────────────
@pytest.mark.parametrize("reason, signal, traded, outcome, stage", [
    ("no_trade:NO_SIGNAL", None, False, "NO_SIGNAL", "NO_SIGNAL"),
    ("no_trade:NONE", None, False, "NO_SIGNAL", "NO_SIGNAL"),
    ("no_trade:REJECTED", None, False, "SIGNAL_REJECTED", "SIGNAL_REJECTED"),
    ("AI_NO_TRADE:LOW_CONFIDENCE", "BUY", False, "AI_REJECTED", None),
    ("rejected:kill_switch=FULL_SYSTEM_STOP", "BUY", False, "RISK_REJECTED", None),
    ("rejected:INSUFFICIENT_EQUITY", "BUY", False, "RISK_REJECTED", None),
    ("POSITION_ALREADY_OPEN — x", "BUY", False, "RISK_REJECTED", None),
    ("rejected:duplicate_signal", "BUY", False, "EXECUTION_REJECTED", None),
    ("rejected:INVALID_CONTRACT:quote is stale (>30s)", "BUY", False, "EXECUTION_REJECTED", None),
    ("submit_error:RuntimeError", "BUY", False, "EXECUTION_REJECTED", None),
    ("market_closed", None, False, "MARKET_CLOSED", None),
    ("candle_fetch_error:RuntimeError", None, False, "DATA_ERROR", None),
    ("scan_error:ValueError", None, False, "SCANNER_ERROR", None),
    ("", "BUY", True, "FILLED", None),
])
def test_outcome_taxonomy_is_distinct_and_consistent(reason, signal, traded, outcome, stage):
    assert ss.derive_outcome(reason, signal, traded) == outcome
    assert outcome in ss.OUTCOMES
    if stage:                                   # Copilot's gate chain must agree with the pipeline
        assert _stage_from_reason(reason) == stage


def test_no_signal_is_never_called_a_rejected_signal_anywhere(worker):
    w, e = _find("NIFTY50", lambda x: x["decision"] is None and x["evaluated"])
    worker.runtime.now_fn = lambda: _now_after_close(w).astimezone(timezone(timedelta(hours=5, minutes=30)))
    worker.db.save_setting("paper_reconcile_ok", "1")
    _scan(worker, _scanner(worker, "NIFTY50", w, _engine(worker, [APPROVE_JSON])))
    pipe, st = _pipeline(worker)
    chain = build_gate_chain_from_db(worker.db)
    assert pipe["outcome"] == "NO_SIGNAL" and chain["stage"] == "NO_SIGNAL"       # ONE answer from both authorities
    assert chain["gates"]["v8d_signal"]["status"] == "NO_SIGNAL"
    assert "REJECT" not in json.dumps({k: chain["gates"][k]["status"] for k in chain["gates"]
                                       if k in ("v8d_signal",)}).upper()
    assert "NO SIGNAL" in chain["human_summary"].upper() and "nothing was rejected" in chain["human_summary"]
    assert chain["diagnostics"]["outcome"] == "NO_SIGNAL"
    # the exact sequence the user asked for
    assert (pipe["latest_signal"], pipe["ai_decision"], pipe["risk_check"], pipe["execution"], pipe["final"]) == \
           ("NO SIGNAL", "NOT EVALUATED", "NOT EVALUATED", "NOT ATTEMPTED", "NO TRADE")


def test_v8d_rejecting_a_valid_setup_is_SIGNAL_REJECTED_not_NO_SIGNAL(worker):
    """A real setup that V8-D then refuses (here: no usable option contract) is a REJECTED signal."""
    w, e = _find("NIFTY50", lambda x: x["decision"] == "CE")
    worker.runtime.now_fn = lambda: _now_after_close(w).astimezone(timezone(timedelta(hours=5, minutes=30)))
    worker.db.save_setting("paper_reconcile_ok", "1")
    eng = _engine(worker, [APPROVE_JSON])
    sc = _scanner(worker, "NIFTY50", w, eng)
    sc.data.chain = [c for c in sc.data.chain if c["option_type"] == "PE"]       # no CE contract at the ATM
    rec = _scan(worker, sc)
    assert rec["traded"] is False and rec["outcome"] == "SIGNAL_REJECTED", (rec["reason"], rec["outcome"])
    assert eng.provider.calls == []                                               # AI not reached
    pipe, _ = _pipeline(worker)
    assert pipe["latest_signal"].startswith("SIGNAL REJECTED") and pipe["ai_decision"] == "NOT EVALUATED"
    assert pipe["risk_check"] == "NOT EVALUATED" and pipe["execution"] == "NOT ATTEMPTED" and pipe["final"] == "NO TRADE"
    assert build_gate_chain_from_db(worker.db)["stage"] == "SIGNAL_REJECTED"


def test_ai_rejected_vs_risk_rejected_vs_filled_are_distinct_outcomes(worker):
    w, _ = _find("NIFTY50", lambda x: x["decision"] == "CE")
    worker.runtime.now_fn = lambda: _now_after_close(w).astimezone(timezone(timedelta(hours=5, minutes=30)))
    worker.db.save_setting("paper_reconcile_ok", "1")
    r1 = _scan(worker, _scanner(worker, "NIFTY50", w, _engine(worker, [REJECT_JSON])))
    assert r1["outcome"] == "AI_REJECTED" and _pipeline(worker)[0]["ai_decision"].startswith("REJECTED")
    from backend.execution.kill_switch import FULL_SYSTEM_STOP
    worker.runtime.kill.set_level(FULL_SYSTEM_STOP, "test")
    r2 = _scan(worker, _scanner(worker, "NIFTY50", w, _engine(worker, [APPROVE_JSON])))
    assert r2["outcome"] == "RISK_REJECTED"


# ── Copilot: deterministic, V8-D-based, and independent of the chat model ───
def test_why_no_trade_routing_and_content(worker):
    for q in ("Why didn't the bot trade?", "Explain the latest rejection", "why no trades today",
              "why is the bot not trading", "why zero signals"):
        assert is_why_no_trade_question(q), q
    for q in ("Show today's trades", "Explain the latest backtest", "What is delta?"):
        assert not is_why_no_trade_question(q), q
    w, e = _find("NIFTY50", lambda x: x["decision"] is None and x["evaluated"])
    worker.runtime.now_fn = lambda: _now_after_close(w).astimezone(timezone(timedelta(hours=5, minutes=30)))
    worker.db.save_setting("paper_reconcile_ok", "1")
    _scan(worker, _scanner(worker, "NIFTY50", w, _engine(worker, [APPROVE_JSON])))
    pipe, _ = _pipeline(worker)
    text = format_why_no_trade({"pipeline": pipe})
    assert "NO SIGNAL" in text and "not a rejection" in text and "the AI was not involved" in text
    assert e["binding_condition"] in text or any(k in text for k in ("trend", "rsi", "reversal", "pullback"))
    assert f"{e['rsi']:.2f}" in text and f"{e['ema20']:.2f}" in text                  # the real numbers
    assert "AI Trading Decision: NOT EVALUATED" in text and "Execution: NOT ATTEMPTED" in text
    assert "separate from the AI Trading Decision gate" in text
    assert "timed out" not in text.lower()


def test_copilot_model_timeout_cannot_hide_or_masquerade_as_the_trading_ai(worker):
    """The Copilot LLM times out, yet 'why no trade' is still answered (deterministically) and the
    answer never blames the AI gate."""
    from backend.copilot.config import CopilotSettings
    from backend.copilot.llm_adapter import LocalOpenAICompatibleAdapter
    from backend.copilot.provider_errors import AIProviderTimeoutError
    w, e = _find("NIFTY50", lambda x: x["decision"] is None and x["evaluated"])
    worker.runtime.now_fn = lambda: _now_after_close(w).astimezone(timezone(timedelta(hours=5, minutes=30)))
    worker.db.save_setting("paper_reconcile_ok", "1")
    _scan(worker, _scanner(worker, "NIFTY50", w, _engine(worker, [APPROVE_JSON])))
    pipe, _ = _pipeline(worker)
    adapter = LocalOpenAICompatibleAdapter(CopilotSettings.__new__(CopilotSettings))
    with mock.patch.object(adapter, "_send_chat", side_effect=AIProviderTimeoutError("model too slow")):
        out = adapter.explain("Why didn't the bot trade?", {"_intent": "GENERAL", "_why_no_trade": True, "pipeline": pipe})
    assert "NO SIGNAL" in out and "the AI was not involved" in out
    # while an ordinary question DOES surface the (Copilot) provider timeout as a typed error
    with mock.patch.object(adapter, "_send_chat", side_effect=AIProviderTimeoutError("model too slow")):
        with pytest.raises(AIProviderTimeoutError):
            adapter.explain("What is gamma?", {"_intent": "EDUCATION"})
    # and the trading AI's state is unaffected by any of this
    assert pipe["ai_decision"] == "NOT EVALUATED"


# ── the quote-age bug ───────────────────────────────────────────────────────
def test_option_quote_age_is_the_chain_snapshot_age_not_the_candle_age(worker):
    """Regression: the underlying candle's age (0-300 s, always >=300 s for a completed bar) was passed
    as the OPTION quote age, so the contract validator ('quote is stale >30s') rejected genuine BUYs."""
    w, _ = _find("NIFTY50", lambda x: x["decision"] == "CE")
    worker.runtime.now_fn = lambda: _now_after_close(w).astimezone(timezone(timedelta(hours=5, minutes=30)))
    worker.db.save_setting("paper_reconcile_ok", "1")
    captured = {}
    real = worker.runtime.submit_entry

    def spy(payload):
        captured.update(payload)
        return real(payload)
    with mock.patch.object(worker.runtime, "submit_entry", side_effect=spy):
        rec = _scan(worker, _scanner(worker, "NIFTY50", w, _engine(worker, [APPROVE_JSON])))
    assert rec["traded"] is True, rec["reason"]
    assert rec["details"]["underlying_candle_age_seconds"] >= 300 - 1            # the completed bar's age
    assert captured["quote_age_seconds"] < 5.0                                   # the OPTION quote's age
    assert rec["details"]["quote_age_seconds"] < 5.0


def test_a_genuinely_stale_option_quote_is_still_rejected(worker):
    """The 30 s limit is unchanged: a slow path that lets the chain snapshot age past 30 s is refused."""
    w, _ = _find("NIFTY50", lambda x: x["decision"] == "CE")
    worker.runtime.now_fn = lambda: _now_after_close(w).astimezone(timezone(timedelta(hours=5, minutes=30)))
    worker.db.save_setting("paper_reconcile_ok", "1")
    import backend.paper.market_scan_loop as msl
    t = {"v": 1000.0}

    def fake_monotonic():
        t["v"] += 20.0                      # each call advances 20 s: snapshot age > 30 s by submit time
        return t["v"]
    with mock.patch.object(msl.time, "monotonic", side_effect=fake_monotonic):
        rec = _scan(worker, _scanner(worker, "NIFTY50", w, _engine(worker, [APPROVE_JSON])))
    assert rec["traded"] is False and "stale" in rec["reason"], rec["reason"]
    assert not worker.db.list_trades()


# ── single execution path: the API-process scanner can no longer execute ────
def test_live_scanner_cannot_submit_orders_and_reports_signal_only():
    from backend.scanner.live_scanner import LiveScanner
    src = (ROOT / "backend/scanner/live_scanner.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    import ast
    names = {n.attr for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Attribute)}
    assert "submit_entry" not in names and "get_paper_runtime" not in names           # AST: no call path at all
    runtime = mock.MagicMock()

    class Eng:
        def evaluate_configured_strategy(self, symbol):
            return SimpleNamespace(strategy_name="V8_D_PULLBACK_ATM", signal="BUY", confidence=80.0,
                                   entry_reason="BUY", rejected_reasons=[], entry_price=85.0,
                                   indicators={"selected_contract": {"instrument_key": "NSE_FO|X", "option_type": "CE", "strike": 24000}},
                                   to_dict=lambda: {"signal": "BUY"})
    with mock.patch("backend.api.routers.bot_control.get_paper_runtime", return_value=runtime), \
         mock.patch("backend.api.routers.bot_control.BotState") as bs:
        bs.is_running.return_value = True
        bs.status.return_value = {"kill_switch_active": False}
        entry = LiveScanner(trading_engine=Eng(), universe_resolver=lambda: ["NIFTY50"]).scan_symbol("NIFTY50")
    assert entry.signal == "BUY" and entry.execution_status == "SIGNAL_ONLY"
    assert "paper worker is the only executor" in entry.decision
    runtime.submit_entry.assert_not_called()                                          # the AI-bypassing path is gone


# ── Option Scanner EMA / RSI / Volume ───────────────────────────────────────
def test_option_scanner_shows_the_real_v8d_indicator_values():
    from backend.scanner.live_scanner import LiveScanner
    strat = V8DStrategy()
    w, e = _find("BANKNIFTY", lambda x: x["decision"] is None and x["evaluated"])
    diag = explain_pullback(w, strat)

    class Eng:
        def evaluate_configured_strategy(self, symbol):
            return SimpleNamespace(strategy_name="V8_D_PULLBACK_ATM", signal="NONE", confidence=0.0,
                                   entry_reason="NO TRADE", rejected_reasons=["Underlying technical pullback/reversal criteria not met"],
                                   entry_price=None, indicators={"v8d_diagnostics": diag, "candle_count": 120},
                                   to_dict=lambda: {})
    d = LiveScanner(trading_engine=Eng(), universe_resolver=lambda: ["BANKNIFTY"]).scan_symbol("BANKNIFTY").to_dict()
    assert d["ema20"] == pytest.approx(diag["ema20"]) and d["ema50"] == pytest.approx(diag["ema50"])
    assert d["rsi_value"] == pytest.approx(diag["rsi"]) and d["candle_count"] == 120
    assert d["ema_status"] in ("PASS", "FAILED") and d["rsi_status"] in ("PASS", "FAILED")      # not N/A any more
    side = diag["closest_side"].lower()
    assert d["ema_status"] == ("PASS" if diag[side]["trend"]["pass"] else "FAILED")
    assert d["rsi_status"] == ("PASS" if diag[side]["rsi"]["pass"] else "FAILED")
    assert d["volume_status"] == "NOT_USED" and "volume is not part of V8-D" in d["indicator_note"]
    assert d["v8d_failed"] == diag["failed"]


def test_option_scanner_is_honest_when_indicators_are_unavailable():
    from backend.scanner.live_scanner import LiveScanner

    class Eng:
        def evaluate_configured_strategy(self, symbol):
            return SimpleNamespace(strategy_name="V8_D_PULLBACK_ATM", signal="NONE", confidence=0.0, entry_reason="x",
                                   rejected_reasons=["Insufficient underlying candles: 12"], entry_price=None,
                                   indicators={}, to_dict=lambda: {})
    d = LiveScanner(trading_engine=Eng(), universe_resolver=lambda: ["NIFTY50"]).scan_symbol("NIFTY50").to_dict()
    assert d["ema_status"] == "N/A" and d["rsi_status"] == "N/A" and d["rsi_value"] is None and d["ema20"] is None
    assert "unavailable" in d["indicator_note"]            # reported honestly, never invented


def test_display_evaluator_attaches_real_diagnostics_and_excludes_the_forming_bar():
    from backend.strategy import trading_engine as te
    strat = V8DStrategy()
    c = _load("NIFTY50")
    i = next(i for i in range(WINDOW, len(c)) if _in_entry_window(c[i - WINDOW + 1:i + 1]))
    w = c[i - WINDOW + 1: i + 1]
    forming = dict(w[-1], timestamp=(datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat())
    series = w[:-1] + [forming]
    client = mock.MagicMock()
    client.get_current_candles.return_value = series
    client.get_nearest_expiry.return_value = "2027-01-07"
    client.get_option_chain_with_spot.return_value = (_chain_for(strat, series[-1]["close"], "NIFTY50"), series[-1]["close"])
    with mock.patch.dict(os.environ, {"TRADING_MODE": "paper", "TRADING_STRATEGY": "V8_D_PULLBACK_ATM"}):
        import backend.config.settings as sm
        te.settings = sm.load_settings()
        eng = te.TradingEngine(client=client)
        sig = eng.evaluate_configured_strategy("NIFTY50")
    d = sig.indicators["v8d_diagnostics"]
    assert d["forming_candle_excluded"] is True and d["candles_evaluated"] == len(series) - 1
    ref = explain_pullback(series[:-1], strat)               # computed WITHOUT the forming bar
    assert d["ema20"] == pytest.approx(ref["ema20"]) and d["rsi"] == pytest.approx(ref["rsi"])
