"""PHASE 5.2 §21 — deterministic Paper AI rehearsal harness (labeled TEST).

Feeds a controlled, fully-labeled TEST V8-D signal through the REAL chain:

    V8-D signal (TEST) → AI decision → hard risk → PositionSizer
        → ExecutionPipeline → PaperBroker → trade ledger

WITHOUT any fake market data entering the production runtime: the rehearsal
builds its own ephemeral SQLite database, its own PaperTradingRuntime, and a
TEST-labeled signal (trade id prefix REHEARSAL-, TEST_REHEARSAL metadata).
Every scenario records the gate-by-gate outcome for the audit trail.

This is the ONLY sanctioned way to exercise AI APPROVE end-to-end without
waiting for a genuine live V8-D signal — and its outputs are never
presented as live trading.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest import mock as _mock

from backend.ai_decision.contract import APPROVE, R_APPROVED
from backend.ai_decision.context import MarketSession, RiskContext
from backend.ai_decision.decision_engine import (
    AITradingDecisionEngine,
    apply_ai_decision_gate,
)
from backend.ai_decision.setup_identity import build_setup_id
from backend.copilot.provider_errors import (
    AIProviderTimeoutError,
    AIProviderUnavailableError,
)
from backend.strategy.signal import SignalType, StrategySignal


REHEARSAL_PREFIX = "REHEARSAL-"


class ScriptedAIProvider:
    """Deterministic scripted AI provider for rehearsals (never a real call)."""

    def __init__(self, outputs: List[Any]) -> None:
        self.outputs = list(outputs)
        self.calls = 0

    def chat_json(self, snapshot: Dict[str, Any]) -> str:
        self.calls += 1
        out = self.outputs.pop(0) if self.outputs else '{"decision": "REJECT"}'
        if isinstance(out, Exception):
            raise out
        return out


def _test_signal(setup_seed: int = 0) -> StrategySignal:
    """TEST-labeled V8-D signal. setup_seed varies the instrument key so
    each rehearsal scenario is a distinct setup (no cross-scenario dedup)."""
    # Expiry must be in the future for the contract validator: nearest
    # Thursday-like rolling expiry at least 3 days out.
    today = datetime.now(timezone.utc).date()
    expiry = (today + timedelta(days=((3 + today.weekday()) % 7) + 4)).isoformat()
    sig = StrategySignal(
        strategy_name="V8_D_PULLBACK_ATM", symbol="NIFTY50", signal=SignalType.BUY,
        entry_price=85.0, stop_loss=61.2, target=120.0,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    sig.conditions = {"ema_trend_pass": True, "pullback_confirmed": True}
    sig.indicators = {
        "selected_contract": {
            "instrument_key": f"NSE_FO|REHEARSAL_CE_{setup_seed}",
            "option_type": "CE", "strike": 24000.0, "lot_size": 75,
            "ltp": 85.0, "bid_price": 84.5, "ask_price": 85.5, "option_atr": 5.0,
        },
        "sizing": {"quantity": 75},
        "underlying_spot": 24050.0,
        "atm_strike": 24000,
        "lot_size": 75,
        "option_type": "CE",
    }
    return sig, expiry


def _rehearsal_runtime(db_path: str):
    from backend.paper.paper_runtime import PaperTradingRuntime
    env = {
        "TRADING_MODE": "paper",
        "TRADING_STRATEGY": "V8_D_PULLBACK_ATM",
        "UPSTOX_ORDER_PRODUCT": "I",
        "DATABASE_PATH": db_path,
        "TRADING_CAPITAL": "100000",
        "RISK_PER_TRADE_PCT": "0.025",
    }
    with _mock.patch.dict(os.environ, env, clear=False):
        rt = PaperTradingRuntime()

    class FakeKill:
        def level(self):
            return "OFF"

        def blocks_entries(self):
            return False

        def requires_flatten(self):
            return False

    rt.kill = FakeKill()
    return rt


def run_rehearsal(scenarios: Optional[List[str]] = None) -> Dict[str, Any]:
    """Run the labeled TEST rehearsal matrix. Returns per-scenario outcomes.

    Scenarios: ai_approve, ai_reject, ai_wait, ai_timeout,
    ai_provider_unavailable, ai_malformed, risk_rejection, duplicate_setup,
    restart_after_approval.
    """
    scenarios = scenarios or [
        "ai_approve", "ai_reject", "ai_wait", "ai_timeout",
        "ai_provider_unavailable", "ai_malformed", "risk_rejection",
        "duplicate_setup", "restart_after_approval",
    ]
    db_path = os.path.join(tempfile.mkdtemp(prefix="ai_rehearsal_"), "rehearsal.db")
    rt = _rehearsal_runtime(db_path)
    engine_db = rt.db
    candles = []
    base = datetime(2026, 9, 18, 5, 0, tzinfo=timezone.utc)
    for i in range(80):
        ts = (base + timedelta(minutes=5 * i)).isoformat()
        candles.append({"timestamp": ts, "open": 24000 + i, "high": 24005 + i,
                        "low": 23995 + i, "close": 24000 + i, "volume": 1000})
    risk_ctx = RiskContext(
        equity=100000.0, open_positions=0, trades_today=0,
        daily_realized_pnl=0.0, kill_switch=False, reconciliation_ok=True,
    )
    session = MarketSession(open=True, is_trading_day=True, label="TEST_REHEARSAL")

    def engine_for(outputs: List[Any]) -> AITradingDecisionEngine:
        e = AITradingDecisionEngine(db=engine_db, provider=ScriptedAIProvider(outputs))
        e.settings["enabled"] = True
        return e

    results: Dict[str, Any] = {"label": "TEST", "mode": "paper", "scenarios": {}}

    def _run(name: str, engine: AITradingDecisionEngine, sig: Any, tag: str,
             expiry: str) -> Any:
        contract = sig.indicators["selected_contract"]
        decision = engine.decide(
            signal_id=f"{REHEARSAL_PREFIX}{tag}",
            setup_id=build_setup_id(signal=sig, contract=contract,
                                    expiry=expiry, candles=candles),
            signal=sig, contract=contract, expiry=expiry, candles=candles,
            candles_fresh=True, candle_age_seconds=2.0,
            risk=risk_ctx, session=session,
            pipeline_strategy="V8_D_PULLBACK_ATM",
        )
        payload = {
            "timestamp": sig.generated_at, "instrument_key": contract["instrument_key"],
            "option_type": "CE", "underlying": "NIFTY50", "strike": 24000.0,
            "expiry": expiry, "lot_size": 75, "premium": 85.0, "spot": 24050.0,
            "quantity": 75, "stop_loss": 61.2, "target": 120.0, "atr": 5.0,
            "quote_age_seconds": 1.0, "side": "BUY", "strategy": "V8_D_PULLBACK_ATM",
            "trade_id": f"{REHEARSAL_PREFIX}{tag}-{decision.decision_id[:8]}",
        }
        gate = apply_ai_decision_gate(payload, decision, pipeline_strategy="V8_D_PULLBACK_ATM")
        outcome: Dict[str, Any] = {
            "ai_decision": decision.decision,
            "ai_reason_codes": list(decision.reason_codes),
            "ai_decision_id": decision.decision_id,
            "gate": gate or "PASS",
            "label": "TEST",
        }
        if gate is None:
            res = rt.submit_entry(payload)
            outcome["submitted"] = bool(getattr(res, "accepted", False))
            outcome["pipeline_reason"] = str(getattr(res, "reason", ""))
            outcome["signal_id"] = str(getattr(res, "signal_id", "") or "")
        else:
            outcome["submitted"] = False
        results["scenarios"][name] = outcome
        return decision

    if "ai_approve" in scenarios:
        sig, expiry = _test_signal(1)
        d = _run("ai_approve", engine_for([
            json.dumps({"decision": "APPROVE", "confidence": 80, "reason_codes": ["OK"]})]),
            sig, "approve", expiry)
        results["scenarios"]["ai_approve"]["trade_recorded"] = bool(rt.db.list_trades())
    if "ai_reject" in scenarios:
        sig, expiry = _test_signal(2)
        _run("ai_reject", engine_for([
            json.dumps({"decision": "REJECT", "confidence": 60, "reason_codes": ["BAD_SETUP"]})]),
            sig, "reject", expiry)
    if "ai_wait" in scenarios:
        sig, expiry = _test_signal(3)
        _run("ai_wait", engine_for([
            json.dumps({"decision": "WAIT", "confidence": 30, "reason_codes": ["AMBIGUOUS"]})]),
            sig, "wait", expiry)
    if "ai_timeout" in scenarios:
        sig, expiry = _test_signal(4)
        _run("ai_timeout", engine_for([AIProviderTimeoutError("rehearsal timeout")]),
             sig, "timeout", expiry)
    if "ai_provider_unavailable" in scenarios:
        sig, expiry = _test_signal(5)
        _run("ai_provider_unavailable",
             engine_for([AIProviderUnavailableError("rehearsal ollama down")]),
             sig, "unavailable", expiry)
    if "ai_malformed" in scenarios:
        sig, expiry = _test_signal(6)
        _run("ai_malformed", engine_for(["this is prose, definitely BUY"]),
             sig, "malformed", expiry)
    if "risk_rejection" in scenarios:
        # Well-formed open BLOCKER position for the same underlying → the
        # pipeline's hard risk gates must reject AFTER AI APPROVE (typed
        # DUPLICATE_POSITION — AI never overrides hard risk).
        sig, expiry = _test_signal(7)
        rt.broker.positions["NSE_FO|BLOCKER"] = {
            "instrument_key": "NSE_FO|BLOCKER", "quantity": 75,
            "average_price": 85.0, "entry_price": 85.0, "mark_price": 85.0,
            "underlying": "NIFTY50", "option_type": "CE", "strike": 24000.0,
            "expiry": expiry, "lot_size": 75, "stop_loss": 61.2, "target": 120.0,
            "trailing_stop": 61.2, "initial_stop": 61.2, "highest_price": 85.0,
            "lowest_price": 85.0, "status": "OPEN", "closed": False,
            "unrealized_pnl": 0.0, "realized_pnl": 0.0, "trade_id": "BLOCKER",
            "entry_time": sig.generated_at, "strategy": "V8_D_PULLBACK_ATM",
            "exit_reason": None, "exit_price": None, "exit_time": None,
        }
        _run("risk_rejection", engine_for([
            json.dumps({"decision": "APPROVE", "confidence": 80, "reason_codes": ["OK"]})]),
            sig, "risk", expiry)
        del rt.broker.positions["NSE_FO|BLOCKER"]
    if "duplicate_setup" in scenarios:
        # Same setup twice (identical bar/instrument) → ONE inference total;
        # the second evaluation replays the stored decision.
        e = engine_for([
            json.dumps({"decision": "APPROVE", "confidence": 80, "reason_codes": ["OK"]})])
        sig, expiry = _test_signal(8)
        d1 = _run("duplicate_setup_first", e, sig, "dup", expiry)
        sig2, _ = _test_signal(8)  # SAME instrument/setup, fresh generated_at
        d2 = _run("duplicate_setup", e, sig2, "dup2", expiry)
        results["scenarios"]["duplicate_setup"] = {
            "label": "TEST",
            "provider_calls_total": e.provider.calls,  # type: ignore[attr-defined]
            "one_inference_proven": e.provider.calls == 1,  # type: ignore[attr-defined]
            "same_decision_id": d1.decision_id == d2.decision_id,
            "ai_decision": d2.decision,
        }
    if "restart_after_approval" in scenarios:
        # New engine instance over the same DB (simulates worker restart).
        e_fresh = AITradingDecisionEngine(
            db=engine_db, provider=ScriptedAIProvider([]))
        e_fresh.settings["enabled"] = True
        sig, expiry = _test_signal(8)  # same setup as duplicate test
        d3 = _run("restart_after_approval", e_fresh, sig, "restart", expiry)
        results["scenarios"]["restart_after_approval"]["replayed_without_inference"] = (
            e_fresh.provider.calls == 0)  # type: ignore[attr-defined]

    # Ledger verification
    trades = rt.db.list_trades()
    results["ledger"] = {
        "trades_total": len(trades),
        "all_test_labeled": all(
            str(t.get("id", "")).startswith(REHEARSAL_PREFIX)
            or "REHEARSAL" in str(t.get("notes", "")) for t in trades),
        "trade_ids": [t.get("id") for t in trades],
    }
    results["db_path"] = db_path
    results["ai_decision_rows"] = len(
        engine_db._connect().execute("SELECT * FROM ai_decisions").fetchall())
    return results


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    out = run_rehearsal()
    print(json.dumps(out, indent=1, default=str)[:4000])
