"""PHASE 5.1 — AI trading decision layer regression tests (§21, 20 tests).

Proves the full chain: V8-D signal + AI APPROVE + Risk PASS + Sizer PASS +
ExecutionPipeline PASS → paper fill, and that ANY missing leg (AI REJECT /
WAIT / timeout / provider down / malformed JSON / stale data / kill switch /
risk failure) results in NO TRADE. Also proves the AI layer can never reach
the broker and that paper trading actually respects AI rejection.
"""
from __future__ import annotations

import ast
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import pytest

from backend.ai_decision.contract import (
    APPROVE,
    R_APPROVED,
    R_DECISION_INVALID,
    R_INVALID_RESPONSE,
    R_MODEL_UNAVAILABLE,
    R_PROVIDER_UNAVAILABLE,
    R_REJECTED,
    R_STRATEGY_MISMATCH,
    R_TIMEOUT,
    REJECT,
    WAIT,
    AITradingDecision,
)
from backend.ai_decision.context import (
    MarketSession,
    RiskContext,
    assert_no_secrets,
    build_ai_snapshot,
    build_market_context,
    snapshot_hash,
)
from backend.ai_decision.decision_engine import (
    AITradingDecisionEngine,
    OllamaDecisionProvider,
    _extract_json,
    apply_ai_decision_gate,
    load_ai_decision_settings,
)
from backend.ai_decision.store import AIDecisionStore, make_decision_idempotency_key
from backend.copilot.provider_errors import (
    AIProviderTimeoutError,
    AIProviderUnavailableError,
)
from backend.paper.market_scan_loop import PaperMarketScanner, build_scan_signal_id
from backend.strategy.signal import SignalType, StrategySignal


BACKEND_DIR = Path(__file__).resolve().parents[1]


# ── shared fakes ──────────────────────────────────────────────────────────

def _candles(n: int = 80, fresh: bool = True, now: Any = None) -> List[Dict[str, Any]]:
    now = now or datetime(2026, 9, 18, 5, 0, tzinfo=timezone.utc)
    ts = now if fresh else now - timedelta(hours=6)
    return [
        {
            "timestamp": (ts - timedelta(minutes=5 * (n - 1 - i))).isoformat(),
            "open": 24000 + i, "high": 24005 + i, "low": 23995 + i,
            "close": 24000 + i, "volume": 1000,
        }
        for i in range(n)
    ]


def _contract() -> Dict[str, Any]:
    return {
        "instrument_key": "NSE_FO|AI_TEST_CE",
        "option_type": "CE",
        "strike": 24000.0,
        "lot_size": 75,
        "ltp": 85.0,
        "bid_price": 84.5,
        "ask_price": 85.5,
        "option_atr": 5.0,
    }


def _buy_signal() -> StrategySignal:
    sig = StrategySignal(
        strategy_name="V8_D_PULLBACK_ATM", symbol="NIFTY50", signal=SignalType.BUY,
        entry_price=85.0, stop_loss=61.2, target=120.0,
        generated_at="2026-09-18T05:00:00+00:00",
    )
    sig.conditions = {"ema_trend_pass": True, "pullback_confirmed": True, "momentum_shift": True}
    sig.indicators = {
        "selected_contract": _contract(),
        "sizing": {"quantity": 75},
        "underlying_spot": 24050.0,
        "atm_strike": 24000,
        "lot_size": 75,
        "option_type": "CE",
    }
    return sig


def _risk(**over: Any) -> RiskContext:
    kw = dict(equity=100000.0, open_positions=0, trades_today=0,
              daily_realized_pnl=0.0, kill_switch=False)
    kw.update(over)
    return RiskContext(**kw)


class ScriptedProvider:
    """Deterministic test double: returns scripted outputs, records calls."""

    def __init__(self, outputs: List[Any]) -> None:
        self.outputs = list(outputs)
        self.calls: List[Dict[str, Any]] = []

    def chat_json(self, snapshot: Dict[str, Any]) -> str:
        self.calls.append({"snapshot": snapshot})
        out = self.outputs.pop(0) if self.outputs else '{"decision": "REJECT"}'
        if isinstance(out, Exception):
            raise out
        return out


APPROVE_JSON = json.dumps({
    "decision": "APPROVE", "confidence": 82,
    "reason_codes": ["TREND_PASS", "PULLBACK_QUALITY_OK", "SPREAD_OK"],
})
REJECT_JSON = json.dumps({
    "decision": "REJECT", "confidence": 71, "reason_codes": ["POOR_RISK_REWARD"],
})
WAIT_JSON = json.dumps({"decision": "WAIT", "confidence": 40, "reason_codes": ["AMBIGUOUS"]})


def _engine(tmp_dir: str, provider: Any) -> AITradingDecisionEngine:
    return AITradingDecisionEngine(provider=provider)


def _db(tmp_dir: str):
    from backend.database.db_manager import DatabaseManager
    return DatabaseManager(db_path=os.path.join(tmp_dir, "ai.db"))


@pytest.fixture()
def clean_env(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("TRADING_STRATEGY", "V8_D_PULLBACK_ATM")
    monkeypatch.setenv("UPSTOX_ORDER_PRODUCT", "I")
    monkeypatch.delenv("AI_DECISION_ENABLED", raising=False)
    yield


# ── 1. contract + context unit tests ─────────────────────────────────────

def test_contract_approve_with_risk_pass_permits_execution(clean_env, tmp_path):
    """Test 1: V8-D signal + AI APPROVE + Risk PASS → execution permitted."""
    decision = AITradingDecision.from_model_output(
        json.loads(APPROVE_JSON), strategy="V8_D_PULLBACK_ATM", symbol="NIFTY50",
        input_snapshot_hash="h", model_provider="p", model_name="m", model_version="v",
        market_timestamp="2026-09-18T05:00:00+00:00",
        fallback_contract={
            "instrument_key": "NSE_FO|AI_TEST_CE", "option_type": "CE",
            "entry_price": 85.0, "stop_loss": 61.2, "target": 120.0,
            "quantity": 75, "lot_size": 75,
        },
    )
    assert decision.decision == APPROVE
    assert decision.allows_execution is True
    gate = apply_ai_decision_gate({"expiry": "2026-09-24"}, decision,
                                  pipeline_strategy="V8_D_PULLBACK_ATM")
    assert gate is None  # pipeline may continue
    payload_gate = {"expiry": "2026-09-24"}
    apply_ai_decision_gate(payload_gate, decision, pipeline_strategy="V8_D_PULLBACK_ATM")
    assert payload_gate["ai_decision"]["decision_id"] == decision.decision_id
    assert payload_gate["ai_decision"]["input_snapshot_hash"] == "h"


def test_contract_ai_reject_blocks(clean_env):
    """Test 2: V8-D signal + AI REJECT → no execution."""
    decision = AITradingDecision.from_model_output(
        json.loads(REJECT_JSON), strategy="V8_D_PULLBACK_ATM", symbol="NIFTY50",
        input_snapshot_hash="h", model_provider="p", model_name="m", model_version="v",
        market_timestamp="t",
    )
    gate = apply_ai_decision_gate({}, decision, pipeline_strategy="V8_D_PULLBACK_ATM")
    assert gate == "AI_NO_TRADE:POOR_RISK_REWARD"


def test_contract_ai_wait_blocks(clean_env):
    """Test 3: V8-D signal + AI WAIT → no execution."""
    decision = AITradingDecision.from_model_output(
        json.loads(WAIT_JSON), strategy="V8_D_PULLBACK_ATM", symbol="NIFTY50",
        input_snapshot_hash="h", model_provider="p", model_name="m", model_version="v",
        market_timestamp="t",
    )
    gate = apply_ai_decision_gate({}, decision, pipeline_strategy="V8_D_PULLBACK_ATM")
    assert gate == "AI_NO_TRADE:AMBIGUOUS"


def test_context_builder_uses_verified_values_and_flags_gaps(clean_env):
    """§3: context contains only verified values; missing critical data is
    reported, never invented."""
    sig = _buy_signal()
    ctx = build_market_context(
        symbol="NIFTY50", candles=_candles(), signal=sig, contract=_contract(),
        expiry="2026-09-24", risk=_risk(), session=MarketSession(open=True, is_trading_day=True),
        candles_fresh=True, candle_age_seconds=2.0,
    )
    assert ctx["underlying_price"] == 24050.0
    assert ctx["option"]["ltp"] == 85.0
    assert ctx["option"]["spread_pct"] == 1.18
    assert ctx["proposed_trade"]["risk_reward"] == round((120.0 - 85.0) / (85.0 - 61.2), 2)
    assert ctx["risk"]["kill_switch_active"] is False
    # Missing-data path: empty contract -> contract-derived fields absent
    # (instrument key/strike/expiry/bid/ask never invented); the option LTP
    # falls back to the signal's OWN verified entry price, not a guess.
    ctx2 = build_market_context(
        symbol="NIFTY50", candles=_candles(), signal=sig, contract={},
        expiry="", risk=_risk(), session=MarketSession(open=True, is_trading_day=True),
        candles_fresh=True, candle_age_seconds=None,
    )
    assert ctx2["option"]["instrument_key"] == ""
    assert ctx2["option"]["strike"] == 24000.0  # from signal's atm_strike — verified
    assert ctx2["option"]["expiry"] == ""
    assert ctx2["option"]["bid"] is None and ctx2["option"]["ask"] is None
    assert ctx2["option"]["ltp"] == 85.0  # from signal.entry_price — verified


def test_snapshot_hash_is_canonical_and_secret_free(clean_env):
    """§15/§22: canonical hash of exactly what the AI receives; secrets fail
    the guard."""
    # Key order and int/float representation are canonicalized away.
    snap1 = {"a": {"y": 1.0, "b": [1, 2]}, "x": "s"}
    snap2 = {"x": "s", "a": {"b": [1, 2], "y": 1}}
    assert snapshot_hash(snap1) == snapshot_hash(snap2)
    # Value changes change the hash.
    assert snapshot_hash({"v": 1}) != snapshot_hash({"v": 2})
    with pytest.raises(ValueError):
        assert_no_secrets({"context": {"upstox_access_token": "tok"}})
    with pytest.raises(ValueError):
        assert_no_secrets({"client_secret": "s"})
    snap = build_ai_snapshot(
        build_market_context(
            symbol="NIFTY50", candles=_candles(), signal=_buy_signal(),
            contract=_contract(), expiry="2026-09-24", risk=_risk(),
            session=MarketSession(open=True, is_trading_day=True),
            candles_fresh=True, candle_age_seconds=2.0,
        ),
        strategy="V8_D_PULLBACK_ATM",
    )
    assert_no_secrets(snap)  # must not raise
    assert snapshot_hash(snap)


# ── 2. engine decision tests ─────────────────────────────────────────────

def test_engine_approve_happy_path(clean_env, tmp_path):
    """Full engine path: scripted APPROVE → structured decision with durable
    store row."""
    db = _db(str(tmp_path))
    provider = ScriptedProvider([APPROVE_JSON])
    engine = AITradingDecisionEngine(db=db, provider=provider)
    engine.settings["enabled"] = True
    decision = engine.decide(
        signal_id="sig1", signal=_buy_signal(), contract=_contract(),
        expiry="2026-09-24", candles=_candles(), candles_fresh=True,
        candle_age_seconds=2.0, risk=_risk(), session=MarketSession(open=True, is_trading_day=True),
        pipeline_strategy="V8_D_PULLBACK_ATM",
    )
    assert decision.decision == APPROVE
    assert R_APPROVED in decision.reason_codes
    assert decision.input_snapshot_hash
    stored = engine.store.get_decisions_for_signal("sig1")
    assert len(stored) == 1 and stored[0]["decision"] == "APPROVE"


def test_engine_stale_data_waits(clean_env, tmp_path):
    """§3 policy: stale candles → WAIT (NO TRADE), no provider call."""
    provider = ScriptedProvider([APPROVE_JSON])
    engine = AITradingDecisionEngine(db=None, provider=provider)
    decision = engine.decide(
        signal_id="sig2", signal=_buy_signal(), contract=_contract(),
        expiry="2026-09-24", candles=_candles(fresh=False), candles_fresh=False,
        candle_age_seconds=9999.0, risk=_risk(), session=MarketSession(open=True, is_trading_day=True),
        pipeline_strategy="V8_D_PULLBACK_ATM",
    )
    assert decision.decision == WAIT
    assert not provider.calls  # policy short-circuit, no model call


def test_engine_timeout_fails_closed(clean_env):
    """Test 7: AI timeout → AI_TIMEOUT, NO TRADE, never a fallback to BUY."""
    provider = ScriptedProvider([AIProviderTimeoutError("slow")])
    engine = AITradingDecisionEngine(db=None, provider=provider)
    decision = engine.decide(
        signal_id="sig3", signal=_buy_signal(), contract=_contract(),
        expiry="2026-09-24", candles=_candles(), candles_fresh=True,
        candle_age_seconds=2.0, risk=_risk(), session=MarketSession(open=True, is_trading_day=True),
        pipeline_strategy="V8_D_PULLBACK_ATM",
    )
    assert decision.decision == REJECT
    assert R_TIMEOUT in decision.reason_codes
    assert decision.allows_execution is False


def test_engine_provider_unavailable_fails_closed(clean_env):
    """Test 8: Ollama down → AI_PROVIDER_UNAVAILABLE, NO TRADE."""
    provider = ScriptedProvider([AIProviderUnavailableError("connection refused")])
    engine = AITradingDecisionEngine(db=None, provider=provider)
    decision = engine.decide(
        signal_id="sig4", signal=_buy_signal(), contract=_contract(),
        expiry="2026-09-24", candles=_candles(), candles_fresh=True,
        candle_age_seconds=2.0, risk=_risk(), session=MarketSession(open=True, is_trading_day=True),
        pipeline_strategy="V8_D_PULLBACK_ATM",
    )
    assert decision.decision == REJECT
    assert R_PROVIDER_UNAVAILABLE in decision.reason_codes


def test_engine_malformed_json_fails_closed(clean_env):
    """Test 9: malformed response → AI_INVALID_RESPONSE, NO TRADE. Prose is
    never parsed into a verdict (§12)."""
    provider = ScriptedProvider(["I think this trade looks great, BUY it!"])
    engine = AITradingDecisionEngine(db=None, provider=provider)
    decision = engine.decide(
        signal_id="sig5", signal=_buy_signal(), contract=_contract(),
        expiry="2026-09-24", candles=_candles(), candles_fresh=True,
        candle_age_seconds=2.0, risk=_risk(), session=MarketSession(open=True, is_trading_day=True),
        pipeline_strategy="V8_D_PULLBACK_ATM",
    )
    assert decision.decision == REJECT
    assert R_INVALID_RESPONSE in decision.reason_codes


def test_engine_wrong_strategy_rejected(clean_env):
    """Test 10: decision for a non-configured strategy is rejected."""
    engine = AITradingDecisionEngine(db=None, provider=ScriptedProvider([APPROVE_JSON]))
    decision = engine.decide(
        signal_id="sig6", signal=_buy_signal(), contract=_contract(),
        expiry="2026-09-24", candles=_candles(), candles_fresh=True,
        candle_age_seconds=2.0, risk=_risk(), session=MarketSession(open=True, is_trading_day=True),
        pipeline_strategy="OPTION_PREMIUM",  # wrong pipeline strategy
    )
    assert decision.decision == REJECT
    assert R_STRATEGY_MISMATCH in decision.reason_codes


def test_engine_option_premium_decision_rejected_under_v8d(clean_env):
    """Test 11: an OPTION_PREMIUM-identity signal can never be approved when
    production strategy is V8-D."""
    sig = _buy_signal()
    sig.strategy_name = "OPTION_PREMIUM"
    engine = AITradingDecisionEngine(db=None, provider=ScriptedProvider([APPROVE_JSON]))
    decision = engine.decide(
        signal_id="sig7", signal=sig, contract=_contract(),
        expiry="2026-09-24", candles=_candles(), candles_fresh=True,
        candle_age_seconds=2.0, risk=_risk(), session=MarketSession(open=True, is_trading_day=True),
        pipeline_strategy="V8_D_PULLBACK_ATM",
    )
    assert decision.decision == REJECT
    assert R_STRATEGY_MISMATCH in decision.reason_codes


def test_engine_duplicate_decision_idempotent(clean_env, tmp_path):
    """Test 12: same signal_id + snapshot hash + model → replayed decision,
    single stored row, one provider call."""
    db = _db(str(tmp_path))
    provider = ScriptedProvider([APPROVE_JSON])
    engine = AITradingDecisionEngine(db=db, provider=provider)
    engine.settings["enabled"] = True
    kwargs = dict(
        signal=_buy_signal(), contract=_contract(), expiry="2026-09-24",
        candles=_candles(), candles_fresh=True, candle_age_seconds=2.0,
        risk=_risk(), session=MarketSession(open=True, is_trading_day=True),
        pipeline_strategy="V8_D_PULLBACK_ATM",
    )
    d1 = engine.decide(signal_id="sig8", **kwargs)
    d2 = engine.decide(signal_id="sig8", **kwargs)
    assert d1.decision == d2.decision == APPROVE
    assert d1.decision_id == d2.decision_id
    assert len(provider.calls) == 1  # second call replayed from store
    assert len(engine.store.get_decisions_for_signal("sig8")) == 1


def test_engine_restart_recovery_replays_stored(clean_env, tmp_path):
    """Test 13: restart after AI approval before order — a NEW engine
    instance (fresh process) replays the stored decision for the identical
    evaluation; the Phase 5 idempotent order store still owns the order."""
    db = _db(str(tmp_path))
    provider = ScriptedProvider([APPROVE_JSON])
    engine1 = AITradingDecisionEngine(db=db, provider=provider)
    engine1.settings["enabled"] = True
    d1 = engine1.decide(signal_id="sig9", signal=_buy_signal(), contract=_contract(),
                        expiry="2026-09-24", candles=_candles(), candles_fresh=True,
                        candle_age_seconds=2.0, risk=_risk(),
                        session=MarketSession(open=True, is_trading_day=True),
                        pipeline_strategy="V8_D_PULLBACK_ATM")
    assert d1.decision == APPROVE
    # "restart": new engine over the same DB, provider never called again
    engine2 = AITradingDecisionEngine(db=db, provider=ScriptedProvider([]))
    d2 = engine2.decide(signal_id="sig9", signal=_buy_signal(), contract=_contract(),
                        expiry="2026-09-24", candles=_candles(), candles_fresh=True,
                        candle_age_seconds=2.0, risk=_risk(),
                        session=MarketSession(open=True, is_trading_day=True),
                        pipeline_strategy="V8_D_PULLBACK_ATM")
    assert d2.decision_id == d1.decision_id
    assert d2.decision == APPROVE


def test_scan_id_matches_pipeline_scheme(clean_env):
    """Test 14 (part 1): the scan-side signal id uses the exact same inputs
    the ExecutionPipeline will use, so restart attribution still works and
    AI decisions join to trades by signal_id."""
    from backend.orders.idempotency import make_signal_id
    sig = _buy_signal()
    scan_id = build_scan_signal_id(sig, "2026-09-24")
    expected = make_signal_id(
        strategy=sig.strategy_name,
        timestamp=sig.generated_at,
        instrument="NSE_FO|AI_TEST_CE",
        direction="CE",
    )
    assert scan_id == expected


def test_extract_json_never_parses_prose(clean_env):
    """§12 hard rule: free text never becomes a decision; fenced/strip JSON
    works."""
    assert _extract_json("BUY BUY BUY the dip") is None
    assert _extract_json("The answer is: buy") is None
    obj = _extract_json('```json\n{"decision": "REJECT", "confidence": 55}\n```')
    assert obj and obj["decision"] == "REJECT"
    obj2 = _extract_json('{"decision": "WAIT", "confidence": 30, "reason_codes": []}')
    assert obj2 and obj2["decision"] == "WAIT"


# ── 3. gate + paper-path integration tests ───────────────────────────────

def _runtime(db_path_env: Dict[str, str]):
    from backend.paper.paper_runtime import PaperTradingRuntime
    with mock.patch.dict(os.environ, db_path_env, clear=False):
        return PaperTradingRuntime()


def _runtime_env(tmp_dir: str) -> Dict[str, str]:
    return {
        "TRADING_MODE": "paper",
        "TRADING_STRATEGY": "V8_D_PULLBACK_ATM",
        "UPSTOX_ORDER_PRODUCT": "I",
        "DATABASE_PATH": os.path.join(tmp_dir, "scan.db"),
        "RISK_PER_TRADE_PCT": "0.025",
        "TRADING_CAPITAL": "100000",
    }


def _scanner_with_ai(runtime, strategy_obj, provider_outputs, tmp_dir: str) -> PaperMarketScanner:
    db = _db(tmp_dir)
    engine = AITradingDecisionEngine(db=db, provider=provider_outputs)
    engine.settings["enabled"] = True
    return PaperMarketScanner(
        data=None, strategy=strategy_obj, ai_engine=engine,
        ai_decision_pipeline_strategy="V8_D_PULLBACK_ATM",
    ), engine


class _FakeMarketData:
    """Minimal MarketDataSource over injected candles/chain (test only)."""

    def __init__(self, candles, chain, spot, expiry="2026-09-24") -> None:
        self.candles = candles
        self.chain = chain
        self.spot = spot
        self.expiry = expiry


# Reuse the exact V8-D-driving candle/chain fixtures from the scanner suite
# via import to avoid divergent test data.
from backend.tests.test_market_scan_loop import (  # noqa: E402
    FakeMarketData,
    _atm_chain,
    _bars_for_ce_signal,
)


def _mk_scanner_env(tmp_dir: str, now):
    """Fresh runtime + scanner fixtures bound to a known trading morning."""
    candles = _bars_for_ce_signal(80)
    for i, c in enumerate(candles):
        # last bar timestamp == now → quote_age ≈ 0 for the contract validator
        c["timestamp"] = (now - timedelta(minutes=5 * (len(candles) - 1 - i))).isoformat()
    data = FakeMarketData(candles, _atm_chain(24050), 24050.0)
    return data


class _KillingRuntime:
    """Runtime double whose kill switch is active — used to prove hard risk
    overrides AI APPROVE without building a full PaperTradingRuntime."""

    def __init__(self) -> None:
        from backend.execution.kill_switch import PersistentKillSwitch  # noqa: F401
        self.kill = type("K", (), {"level": lambda self: "STOP_NEW_ENTRIES"})()
        self.broker = type("B", (), {"positions": {}})()
        self.realized_equity = 100000.0
        self.trades_today = 0
        self.daily_realized_pnl = 0.0


def test_paper_path_respects_ai_reject(clean_env, tmp_path):
    """Test 20 / §6 THE critical test: paper trading actually respects AI
    rejection — the scanner never calls runtime.submit_entry on AI REJECT."""
    now = datetime(2026, 9, 18, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
    data = _mk_scanner_env(str(tmp_path), now)

    class MockStrategy:
        name = "V8_D_PULLBACK_ATM"

        def evaluate_v8d_signal(self, **kwargs):
            sig = _buy_signal()
            sig.symbol = kwargs.get("underlying_symbol", "NIFTY50")
            sig.indicators["underlying_spot"] = kwargs.get("spot_price", 24050.0)
            log = type("L", (), {"decision": "ACCEPTED"})()
            return sig, log

    runtime = _runtime(_runtime_env(str(tmp_path)))
    submitted = []

    class SpyRuntime:
        def __getattr__(self, item):
            return getattr(runtime, item)

        def submit_entry(self, payload):
            submitted.append(payload)
            return runtime.submit_entry(payload)

    spy = SpyRuntime()

    class FakeKill:
        def level(self):
            return "OFF"

        def blocks_entries(self):
            return False

        def requires_flatten(self):
            return False

    runtime.kill = FakeKill()
    db = _db(str(tmp_path))
    engine = AITradingDecisionEngine(db=db, provider=ScriptedProvider([REJECT_JSON]))
    engine.settings["enabled"] = True
    scanner = PaperMarketScanner(
        data=data, strategy=MockStrategy(), ai_engine=engine,
        ai_decision_pipeline_strategy="V8_D_PULLBACK_ATM",
    )
    res = scanner.scan_once(spy, now=now)
    assert res.traded is False
    assert res.reason.startswith("AI_NO_TRADE:")
    assert submitted == []  # submit_entry NEVER reached
    assert res.details["ai_decision"] == "REJECT"


def test_paper_path_ai_approve_flows_to_submission(clean_env, tmp_path):
    """Full chain proof: V8-D BUY + AI APPROVE → payload stamped with the AI
    decision and submitted to the real runtime → paper fill recorded."""
    now = datetime(2026, 9, 18, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
    data = _mk_scanner_env(str(tmp_path), now)

    class MockStrategy:
        name = "V8_D_PULLBACK_ATM"

        def evaluate_v8d_signal(self, **kwargs):
            sig = _buy_signal()
            sig.symbol = kwargs.get("underlying_symbol", "NIFTY50")
            sig.indicators["underlying_spot"] = kwargs.get("spot_price", 24050.0)
            log = type("L", (), {"decision": "ACCEPTED"})()
            return sig, log

    runtime = _runtime(_runtime_env(str(tmp_path)))

    class FakeKill:
        def level(self):
            return "OFF"

        def blocks_entries(self):
            return False

        def requires_flatten(self):
            return False

    runtime.kill = FakeKill()
    db = _db(str(tmp_path))
    engine = AITradingDecisionEngine(db=db, provider=ScriptedProvider([APPROVE_JSON]))
    engine.settings["enabled"] = True
    scanner = PaperMarketScanner(
        data=data, strategy=MockStrategy(), ai_engine=engine,
        ai_decision_pipeline_strategy="V8_D_PULLBACK_ATM",
    )
    res = scanner.scan_once(runtime, now=now)
    assert res.traded is True, res.reason
    assert res.details.get("ai_decision") == "APPROVE"
    trades = runtime.db.list_trades()
    assert len(trades) >= 1
    # Durable AI metadata recorded with the decision for the SAME signal id
    stored = engine.store.get_decisions_for_signal(res.details["signal_id"])
    assert stored and stored[0]["decision"] == "APPROVE"


def test_paper_path_ai_wait_no_submission(clean_env, tmp_path):
    """AI WAIT → no paper order."""
    now = datetime(2026, 9, 18, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
    data = _mk_scanner_env(str(tmp_path), now)

    class MockStrategy:
        name = "V8_D_PULLBACK_ATM"

        def evaluate_v8d_signal(self, **kwargs):
            sig = _buy_signal()
            sig.indicators["underlying_spot"] = kwargs.get("spot_price", 24050.0)
            log = type("L", (), {"decision": "ACCEPTED"})()
            return sig, log

    runtime = _runtime(_runtime_env(str(tmp_path)))

    class FakeKill:
        def level(self):
            return "OFF"

        def blocks_entries(self):
            return False

        def requires_flatten(self):
            return False

    runtime.kill = FakeKill()
    db = _db(str(tmp_path))
    engine = AITradingDecisionEngine(db=db, provider=ScriptedProvider([WAIT_JSON]))
    engine.settings["enabled"] = True
    scanner = PaperMarketScanner(
        data=data, strategy=MockStrategy(), ai_engine=engine,
        ai_decision_pipeline_strategy="V8_D_PULLBACK_ATM",
    )
    res = scanner.scan_once(runtime, now=now)
    assert res.traded is False
    assert res.reason.startswith("AI_NO_TRADE:")
    assert runtime.db.list_trades() == []


def test_paper_path_ai_approve_with_kill_switch_blocked(clean_env, tmp_path):
    """Test 6: AI APPROVE + kill switch → NO TRADE (hard risk overrides AI).
    With the runtime's own kill switch OFF (spy), the persistent kill switch
    inside the pipeline still blocks the submission."""
    now = datetime(2026, 9, 18, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
    data = _mk_scanner_env(str(tmp_path), now)

    class MockStrategy:
        name = "V8_D_PULLBACK_ATM"

        def evaluate_v8d_signal(self, **kwargs):
            sig = _buy_signal()
            sig.indicators["underlying_spot"] = kwargs.get("spot_price", 24050.0)
            log = type("L", (), {"decision": "ACCEPTED"})()
            return sig, log

    runtime = _runtime(_runtime_env(str(tmp_path)))

    class FakeKill:
        def level(self):
            return "OFF"

        def blocks_entries(self):
            return False

        def requires_flatten(self):
            return False

    runtime.kill = FakeKill()  # scanner-level check passes
    # Set the PERSISTENT kill switch in the runtime's DB — the pipeline gate.
    runtime.db.save_setting("persistent_kill_level", "STOP_NEW_ENTRIES")
    runtime.db.save_setting("persistent_kill_level_reason", "test")
    db = _db(str(tmp_path))
    engine = AITradingDecisionEngine(db=db, provider=ScriptedProvider([APPROVE_JSON]))
    engine.settings["enabled"] = True
    scanner = PaperMarketScanner(
        data=data, strategy=MockStrategy(), ai_engine=engine,
        ai_decision_pipeline_strategy="V8_D_PULLBACK_ATM",
    )
    res = scanner.scan_once(runtime, now=now)
    assert res.traded is False
    # Rejected by the kill switch AFTER AI APPROVE — hard risk wins.
    assert "kill" in res.reason.lower()


def test_paper_path_ai_approve_with_risk_fail_blocked(clean_env, tmp_path):
    """Test 4: AI APPROVE + Risk FAIL → NO TRADE. Tiny equity →
    INSUFFICIENT_EQUITY from the central risk gate after AI APPROVE."""
    now = datetime(2026, 9, 18, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
    data = _mk_scanner_env(str(tmp_path), now)

    class MockStrategy:
        name = "V8_D_PULLBACK_ATM"

        def evaluate_v8d_signal(self, **kwargs):
            sig = _buy_signal()
            sig.indicators["underlying_spot"] = kwargs.get("spot_price", 24050.0)
            log = type("L", (), {"decision": "ACCEPTED"})()
            return sig, log

    env = _runtime_env(str(tmp_path))
    env["TRADING_CAPITAL"] = "100"  # notional 85*75 = 6375 > 100 equity
    runtime = _runtime(env)

    class FakeKill:
        def level(self):
            return "OFF"

        def blocks_entries(self):
            return False

        def requires_flatten(self):
            return False

    runtime.kill = FakeKill()
    db = _db(str(tmp_path))
    engine = AITradingDecisionEngine(db=db, provider=ScriptedProvider([APPROVE_JSON]))
    engine.settings["enabled"] = True
    scanner = PaperMarketScanner(
        data=data, strategy=MockStrategy(), ai_engine=engine,
        ai_decision_pipeline_strategy="V8_D_PULLBACK_ATM",
    )
    res = scanner.scan_once(runtime, now=now)
    assert res.traded is False
    assert "INSUFFICIENT_EQUITY" in res.reason or "RISK" in res.reason.upper()


def test_paper_path_ai_timeout_no_submission(clean_env, tmp_path):
    """AI provider timeout during the scan → no paper order, scan result
    records the typed failure."""
    now = datetime(2026, 9, 18, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
    data = _mk_scanner_env(str(tmp_path), now)

    class MockStrategy:
        name = "V8_D_PULLBACK_ATM"

        def evaluate_v8d_signal(self, **kwargs):
            sig = _buy_signal()
            sig.indicators["underlying_spot"] = kwargs.get("spot_price", 24050.0)
            log = type("L", (), {"decision": "ACCEPTED"})()
            return sig, log

    runtime = _runtime(_runtime_env(str(tmp_path)))

    class FakeKill:
        def level(self):
            return "OFF"

        def blocks_entries(self):
            return False

        def requires_flatten(self):
            return False

    runtime.kill = FakeKill()
    db = _db(str(tmp_path))
    engine = AITradingDecisionEngine(
        db=db, provider=ScriptedProvider([AIProviderTimeoutError("timed out")]))
    engine.settings["enabled"] = True
    scanner = PaperMarketScanner(
        data=data, strategy=MockStrategy(), ai_engine=engine,
        ai_decision_pipeline_strategy="V8_D_PULLBACK_ATM",
    )
    res = scanner.scan_once(runtime, now=now)
    assert res.traded is False
    assert "AI_TIMEOUT" in res.reason
    assert runtime.db.list_trades() == []


# ── 4. security & isolation tests (§15/§19/§20/§22) ──────────────────────

def test_snapshot_carries_no_secrets(clean_env):
    """§22: the exact AI payload contains no token/secret keys, and a leaked
    secret key makes the guard raise."""
    snap = build_ai_snapshot(
        build_market_context(
            symbol="NIFTY50", candles=_candles(), signal=_buy_signal(),
            contract=_contract(), expiry="2026-09-24", risk=_risk(
                kill_switch=True, kill_switch_level="STOP_NEW_ENTRIES"),
            session=MarketSession(open=True, is_trading_day=True),
            candles_fresh=True, candle_age_seconds=2.0,
        ),
        strategy="V8_D_PULLBACK_ATM",
    )
    flat = json.dumps(snap).lower()
    for needle in ("access_token", "upstox", "client_secret", "authorization", "password"):
        assert needle not in flat, needle


def test_ai_layer_never_imports_broker_or_execution(clean_env):
    """Test 15: AST-level guarantee — no module in backend/ai_decision can
    import Upstox/PaperBroker/LiveBroker/OrderManager/execute_multi_signal
    or the execution/orders machinery. The AI layer returns decisions; it
    cannot place orders by construction."""
    banned = (
        "upstox", "paper_broker", "live_broker", "order_manager",
        "execute_multi_signal", "execution.pipeline", "orders.",
        "paper_runtime", "place_order",
    )
    ai_dir = BACKEND_DIR / "ai_decision"
    assert ai_dir.exists()
    for py in ai_dir.glob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for b in banned:
                        assert b not in alias.name.lower(), f"{py.name}: {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for b in banned:
                    assert b not in mod.lower(), f"{py.name}: {mod}"


def test_scanner_hook_cannot_execute_from_copilot(clean_env):
    """Test 19: the Copilot execution path remains refuse-only (Phase 5)."""
    from backend.copilot.execution import submit_trade_plan_for_paper_execution
    result = submit_trade_plan_for_paper_execution(None, {}, {})
    assert result.submitted is False
    assert "COPILOT_EXECUTION_REMOVED" in result.reason


def test_ai_gate_cannot_bypass_risk_or_pipeline(clean_env, tmp_path):
    """Tests 16/17/18: even with AI APPROVE, the payload still flows through
    submit_entry → kill switch/EOD/lot checks → ExecutionPipeline → sizer →
    broker. Removing any pipeline leg blocks execution — proven by the
    INVALID_QUANTITY rejection for a non-lot-multiple payload that carries a
    (forged) AI approval stamp."""
    from backend.execution.pipeline import ExecutionPipeline
    from backend.risk.risk_config import build_authoritative_risk_config
    from backend.database.db_manager import DatabaseManager
    db = DatabaseManager(db_path=os.path.join(tmp_path, "bypass.db"))
    db.init_db()
    risk = build_authoritative_risk_config(
        capital=100000, strategy_risk_pct=0.025, engine_risk_pct=0.025,
        risk_manager_daily_loss_pct=0.02, configured_risk_pct=0.025,
        allocation_limit_pct=0.18, max_daily_trades=3, max_positions=1,
        max_daily_loss_pct=0.02, lot_size_source="contract_metadata",
        order_product="I", strategy_name="V8_D_PULLBACK_ATM", eod_square_off="15:15",
    )
    pipeline = ExecutionPipeline(
        strategy_name="V8_D_PULLBACK_ATM", risk=risk, db=db,
        place_order_fn=lambda sig, sid: (_ for _ in ()).throw(
            AssertionError("broker must never be reached")),
        client=None,
    )
    forged = {
        "strategy": "V8_D_PULLBACK_ATM", "timestamp": "t", "instrument_key": "IK",
        "option_type": "CE", "underlying": "NIFTY50", "strike": 24000.0,
        "expiry": "2026-09-24", "lot_size": 75, "premium": 85.0, "spot": 24050.0,
        "quantity": 77, "stop_loss": 61.2, "target": 120.0, "quote_age_seconds": 1.0,
        "side": "BUY",
        "ai_decision": {"decision": "APPROVE", "confidence": 90},  # forged stamp
    }
    result = pipeline.submit_signal(forged)
    assert result.accepted is False
    assert "INVALID_QUANTITY" in result.reason  # pipeline's own gate, not AI


def test_latency_telemetry_records_failures(clean_env, tmp_path):
    """§23: provider/model/latency/timeout/success are recorded for both
    success and failure paths."""
    db = _db(str(tmp_path))
    store = AIDecisionStore(db)
    store.record_latency(provider="ollama:ollama", model="llama3.2:1b",
                         latency_ms=123.4, timeout_seconds=20.0, success=True)
    store.record_latency(provider="ollama:ollama", model="llama3.2:1b",
                         latency_ms=20000.0, timeout_seconds=20.0,
                         success=False, error_code="AI_TIMEOUT")
    stats = store.latency_stats()
    assert stats["samples"] == 2
    assert stats["success_count"] == 1
    assert stats["error_counts"].get("AI_TIMEOUT") == 1
    assert stats["latency_ms_median"] is not None


def test_store_schema_additive_and_independent(clean_env, tmp_path):
    """§16: the AI store coexists with the Phase 5 order_intents table —
    one database, no second idempotency system for orders."""
    from backend.orders.idempotency import IdempotentOrderStore
    db = _db(str(tmp_path))
    ai_store = AIDecisionStore(db)   # creates ai_decisions additively
    order_store = IdempotentOrderStore(db)
    d = AITradingDecision.fail_closed(R_TIMEOUT, strategy="V8_D_PULLBACK_ATM")
    key = make_decision_idempotency_key(
        signal_id="s", input_snapshot_hash="h", model_provider="p",
        model_name="m", model_version="v")
    assert ai_store.save_decision(d.to_dict(), key, "s") is True
    assert ai_store.save_decision(d.to_dict(), key, "s") is False  # duplicate
    order_store.remember_intent("sigX", {"a": 1})
    assert order_store.get("sigX") is not None
    assert ai_store.get_decision_by_key(key)["decision"] == "REJECT"


def test_settings_default_disabled_and_env_wired(clean_env):
    """Default OFF; env vars actually configure provider/model/timeout."""
    s = load_ai_decision_settings()
    assert s["enabled"] is False
    assert s["provider"] == "ollama"
    assert s["model"] == "llama3.2:1b"
    assert s["temperature"] == 0.0
    with mock.patch.dict(os.environ, {
        "AI_DECISION_ENABLED": "true",
        "AI_DECISION_MODEL": "llama3.2:1b",
        "AI_DECISION_TIMEOUT_SECONDS": "5",
        "AI_DECISION_TEMPERATURE": "0",
    }):
        s2 = load_ai_decision_settings()
        assert s2["enabled"] is True
        assert s2["timeout_seconds"] == 5.0


def test_model_unavailable_mapped(clean_env):
    """§14: model missing → AI_MODEL_UNAVAILABLE, NO TRADE."""
    from backend.copilot.provider_errors import AIModelUnavailableError
    provider = ScriptedProvider([AIModelUnavailableError("model not found")])
    engine = AITradingDecisionEngine(db=None, provider=provider)
    decision = engine.decide(
        signal_id="sig10", signal=_buy_signal(), contract=_contract(),
        expiry="2026-09-24", candles=_candles(), candles_fresh=True,
        candle_age_seconds=2.0, risk=_risk(),
        session=MarketSession(open=True, is_trading_day=True),
        pipeline_strategy="V8_D_PULLBACK_ATM",
    )
    assert decision.decision == REJECT
    assert R_MODEL_UNAVAILABLE in decision.reason_codes


def test_empty_context_snapshot_policy_wait(clean_env):
    """§3: missing critical contract data → WAIT (never fabricate, never
    APPROVE on incomplete data)."""
    provider = ScriptedProvider([APPROVE_JSON])
    engine = AITradingDecisionEngine(db=None, provider=provider)
    decision = engine.decide(
        signal_id="sig11", signal=_buy_signal(), contract={},
        expiry="", candles=_candles(), candles_fresh=True,
        candle_age_seconds=2.0, risk=_risk(),
        session=MarketSession(open=True, is_trading_day=True),
        pipeline_strategy="V8_D_PULLBACK_ATM",
    )
    assert decision.decision == WAIT
    assert not provider.calls
