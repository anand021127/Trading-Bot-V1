"""PHASE 5.2 — AI reliability, dedup, persistence, reconciliation tests.

Covers: setup-identity deduplication (§2/§3), fail-closed persistence
(§4), real reconciliation state (§5), snapshot-hash determinism (§7),
warmup safety (§8/§25), provider failure taxonomy (§23), and restart
safety of stored AI decisions (§22).
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest import mock

import pytest

from backend.ai_decision.contract import (
    APPROVE,
    R_PERSISTENCE_FAILED,
    R_TIMEOUT,
    REJECT,
    WAIT,
    AITradingDecision,
)
from backend.ai_decision.context import (
    MarketSession,
    RiskContext,
    build_ai_snapshot,
    build_market_context,
    snapshot_hash,
)
from backend.ai_decision.decision_engine import (
    AITradingDecisionEngine,
    OllamaDecisionProvider,
    apply_ai_decision_gate,
)
from backend.ai_decision.setup_identity import (
    SETUP_IDENTITY_VERSION,
    build_setup_id,
    last_candle_timestamp,
)
from backend.ai_decision.store import AIDecisionStore, make_decision_idempotency_key
from backend.copilot.provider_errors import AIProviderUnavailableError
from backend.strategy.signal import SignalType, StrategySignal


# ── fixtures ──────────────────────────────────────────────────────────────

def _candles(n: int = 80, bar_ts: str = "2026-09-18T05:00:00+00:00") -> List[Dict[str, Any]]:
    base = datetime.fromisoformat(bar_ts)
    return [
        {
            "timestamp": (base - timedelta(minutes=5 * (n - 1 - i))).isoformat(),
            "open": 24000 + i, "high": 24005 + i, "low": 23995 + i,
            "close": 24000 + i, "volume": 1000,
        }
        for i in range(n)
    ]


def _contract() -> Dict[str, Any]:
    return {
        "instrument_key": "NSE_FO|P52_CE", "option_type": "CE", "strike": 24000.0,
        "lot_size": 75, "ltp": 85.0, "bid_price": 84.5, "ask_price": 85.5, "option_atr": 5.0,
    }


def _signal(generated_at: str = "2026-09-18T05:00:01+00:00") -> StrategySignal:
    sig = StrategySignal(
        strategy_name="V8_D_PULLBACK_ATM", symbol="NIFTY50", signal=SignalType.BUY,
        entry_price=85.0, stop_loss=61.2, target=120.0, generated_at=generated_at,
    )
    sig.conditions = {"ema_trend_pass": True, "pullback_confirmed": True}
    sig.indicators = {
        "selected_contract": _contract(), "sizing": {"quantity": 75},
        "underlying_spot": 24050.0, "atm_strike": 24000, "lot_size": 75, "option_type": "CE",
    }
    return sig


APPROVE_JSON = json.dumps({"decision": "APPROVE", "confidence": 80, "reason_codes": ["OK_SETUP"]})
WAIT_JSON = json.dumps({"decision": "WAIT", "confidence": 30, "reason_codes": ["AMBIGUOUS"]})


class CountingProvider:
    """Counts real inference calls — the dedup assertions use it."""

    def __init__(self, outputs: List[Any]) -> None:
        self.outputs = list(outputs)
        self.calls = 0

    def chat_json(self, snapshot: Dict[str, Any]) -> str:
        self.calls += 1
        out = self.outputs.pop(0) if self.outputs else '{"decision": "REJECT"}'
        if isinstance(out, Exception):
            raise out
        return out


def _db(tmp_dir: str, name: str = "ai.db"):
    from backend.database.db_manager import DatabaseManager
    return DatabaseManager(db_path=os.path.join(tmp_dir, name))


def _risk(**over: Any) -> RiskContext:
    kw = dict(equity=100000.0, open_positions=0, trades_today=0,
              daily_realized_pnl=0.0, kill_switch=False, reconciliation_ok=True)
    kw.update(over)
    return RiskContext(**kw)


SESSION = MarketSession(open=True, is_trading_day=True, label="PAPER_SCAN")


# ── §2/§3 setup identity ──────────────────────────────────────────────────

def test_setup_id_stable_across_scan_ticks(tmp_path):
    """Same continuing setup (fresh generated_at each tick) → SAME setup id.
    This is the noise the old signal_id-based identity suffered from."""
    s1 = build_setup_id(signal=_signal(generated_at="2026-09-18T05:00:01+00:00"),
                        contract=_contract(), expiry="2026-09-24",
                        candles=_candles())
    s2 = build_setup_id(signal=_signal(generated_at="2026-09-18T05:02:33+00:00"),
                        contract=_contract(), expiry="2026-09-24",
                        candles=_candles())
    assert s1 == s2
    assert s1 and len(s1) == 32


def test_setup_id_changes_for_genuinely_new_setup(tmp_path):
    """New market bar / different contract / different direction → NEW id."""
    base = dict(signal=_signal(), contract=_contract(), expiry="2026-09-24",
                candles=_candles())
    sid = build_setup_id(**base)

    newer = dict(base)
    newer["candles"] = _candles(bar_ts="2026-09-18T05:05:00+00:00")  # new bar
    assert build_setup_id(**newer) != sid

    other_contract = dict(base)
    oc = _contract()
    oc["instrument_key"] = "NSE_FO|P52_PE"
    oc["option_type"] = "PE"
    other_contract["contract"] = oc
    assert build_setup_id(**other_contract) != sid

    other_expiry = dict(base)
    other_expiry["expiry"] = "2026-10-29"
    assert build_setup_id(**other_expiry) != sid

    other_dir = dict(base)
    sig_pe = _signal()
    sig_pe.signal = SignalType.SELL
    other_dir["signal"] = sig_pe
    assert build_setup_id(**other_dir) != sid


def test_same_setup_10_scans_one_inference(tmp_path):
    """§3 THE dedup test: same setup repeated 10 times → exactly ONE
    provider inference; the stored decision is replayed every scan."""
    db = _db(str(tmp_path))
    provider = CountingProvider([APPROVE_JSON])
    engine = AITradingDecisionEngine(db=db, provider=provider)
    engine.settings["enabled"] = True
    candles = _candles()

    decisions = []
    for i in range(10):
        d = engine.decide(
            signal_id=f"pipeline_sig_{i}",  # pipeline id differs every tick
            signal=_signal(generated_at=f"2026-09-18T05:{i:02d}:01+00:00"),
            contract=_contract(), expiry="2026-09-24", candles=candles,
            candles_fresh=True, candle_age_seconds=2.0 + i * 3.0,
            risk=_risk(), session=SESSION,
            pipeline_strategy="V8_D_PULLBACK_ATM",
        )
        decisions.append(d)

    assert provider.calls == 1, f"expected 1 inference, got {provider.calls}"
    assert {d.decision for d in decisions} == {APPROVE}
    assert len({d.decision_id for d in decisions}) == 1  # replayed, not re-decided


def test_new_bar_allows_new_inference(tmp_path):
    """§2/§3: a genuinely new setup (new market bar) → new inference."""
    db = _db(str(tmp_path))
    provider = CountingProvider([APPROVE_JSON, APPROVE_JSON])
    engine = AITradingDecisionEngine(db=db, provider=provider)
    engine.settings["enabled"] = True

    d1 = engine.decide(signal_id="s1", signal=_signal(), contract=_contract(),
                       expiry="2026-09-24", candles=_candles(bar_ts="2026-09-18T05:00:00+00:00"),
                       candles_fresh=True, candle_age_seconds=2.0, risk=_risk(),
                       session=SESSION, pipeline_strategy="V8_D_PULLBACK_ATM")
    d2 = engine.decide(signal_id="s2", signal=_signal(), contract=_contract(),
                       expiry="2026-09-24", candles=_candles(bar_ts="2026-09-18T05:05:00+00:00"),
                       candles_fresh=True, candle_age_seconds=2.0, risk=_risk(),
                       session=SESSION, pipeline_strategy="V8_D_PULLBACK_ATM")
    assert provider.calls == 2
    assert d1.decision_id != d2.decision_id


def test_risk_state_change_does_not_retrigger_inference(tmp_path):
    """The setup verdict persists even as equity/trades_today drift between
    scans — hard risk is re-evaluated downstream at execution time anyway."""
    db = _db(str(tmp_path))
    provider = CountingProvider([APPROVE_JSON])
    engine = AITradingDecisionEngine(db=db, provider=provider)
    engine.settings["enabled"] = True
    candles = _candles()
    engine.decide(signal_id="a", signal=_signal(), contract=_contract(),
                  expiry="2026-09-24", candles=candles, candles_fresh=True,
                  candle_age_seconds=2.0, risk=_risk(), session=SESSION,
                  pipeline_strategy="V8_D_PULLBACK_ATM")
    d2 = engine.decide(signal_id="b", signal=_signal(), contract=_contract(),
                       expiry="2026-09-24", candles=candles, candles_fresh=True,
                       candle_age_seconds=4.0, risk=_risk(trades_today=1, equity=99000.0),
                       session=SESSION, pipeline_strategy="V8_D_PULLBACK_ATM")
    assert provider.calls == 1
    assert d2.decision == APPROVE


# ── §4 persistence fail-closed ────────────────────────────────────────────

class BrokenDB:
    """DB double whose writes fail (locked / unavailable / schema error)."""

    def __init__(self, inner: Any, mode: str) -> None:
        self.inner = inner
        self.mode = mode
        self.init_failed = False

    def _connect(self):
        conn = self.inner._connect()

        class BrokenConn:
            def __init__(self, outer):
                self.outer = outer

            def executescript(self, sql):
                if self.outer.mode in ("schema", "unavailable"):
                    raise sqlite3.OperationalError("unable to open database file")

            def execute(self, sql, *a, **k):
                if sql.strip().upper().startswith("SELECT"):
                    return conn.execute(sql, *a, **k)
                if self.outer.mode == "locked":
                    raise sqlite3.OperationalError("database is locked")
                if self.outer.mode == "schema":
                    raise sqlite3.OperationalError("no such table: ai_decisions")
                if self.outer.mode == "write_timeout":
                    raise TimeoutError("commit timed out")
                raise sqlite3.OperationalError("db unavailable")

            def commit(self):
                if self.outer.mode != "read_only_ok":
                    raise sqlite3.OperationalError("cannot commit")

        return BrokenConn(self)

    def get_setting(self, key, default=""):
        return self.inner.get_setting(key, default)

    def save_setting(self, key, value):
        return self.inner.save_setting(key, value)


@pytest.mark.parametrize("mode", ["unavailable", "locked", "schema", "write_timeout"])
def test_persistence_failure_fails_closed(tmp_path, mode):
    """§4: any persistence failure → AI_DECISION_PERSISTENCE_FAILED →
    NO TRADE. An unrecorded APPROVE must never execute."""
    inner = _db(str(tmp_path))
    provider = CountingProvider([APPROVE_JSON])
    engine = AITradingDecisionEngine(db=BrokenDB(inner, mode), provider=provider)
    engine.settings["enabled"] = True
    d = engine.decide(signal_id="p1", signal=_signal(), contract=_contract(),
                      expiry="2026-09-24", candles=_candles(), candles_fresh=True,
                      candle_age_seconds=2.0, risk=_risk(), session=SESSION,
                      pipeline_strategy="V8_D_PULLBACK_ATM")
    assert d.decision == REJECT
    assert R_PERSISTENCE_FAILED in d.reason_codes
    assert d.allows_execution is False
    # The gate must block it.
    reason = apply_ai_decision_gate({}, d, pipeline_strategy="V8_D_PULLBACK_ATM")
    assert reason == "AI_NO_TRADE:AI_DECISION_PERSISTENCE_FAILED"


def test_persistence_failure_on_wait_is_not_fatal(tmp_path):
    """WAIT/REJECT are no-trade regardless — persistence failure must not
    transform them into a crash loop."""
    inner = _db(str(tmp_path))
    provider = CountingProvider([WAIT_JSON])
    engine = AITradingDecisionEngine(db=BrokenDB(inner, "locked"), provider=provider)
    engine.settings["enabled"] = True
    d = engine.decide(signal_id="p2", signal=_signal(), contract=_contract(),
                      expiry="2026-09-24", candles=_candles(), candles_fresh=True,
                      candle_age_seconds=2.0, risk=_risk(), session=SESSION,
                      pipeline_strategy="V8_D_PULLBACK_ATM")
    assert d.decision == WAIT
    assert d.allows_execution is False


def test_store_reports_availability(tmp_path):
    inner = _db(str(tmp_path))
    store = AIDecisionStore(inner)
    assert store.available is True
    broken = AIDecisionStore(BrokenDB(inner, "schema"))
    assert broken.available is False


# ── §5 reconciliation state ──────────────────────────────────────────────

def _scanner_ai_engine(tmp_dir: str, provider_outputs: List[Any]):
    db = _db(tmp_dir)
    engine = AITradingDecisionEngine(db=db, provider=CountingProvider(provider_outputs))
    engine.settings["enabled"] = True
    return engine


class _FakeKillFull:
    def level(self):
        return "OFF"

    def blocks_entries(self):
        return False

    def requires_flatten(self):
        return False


class _ForcedRecDB:
    """DB wrapper that forces the paper_reconcile_ok setting (the scanner
    reads the runtime's persisted reconcile verdict through runtime.db)."""

    def __init__(self, inner: Any, value: str) -> None:
        self._inner = inner
        self._value = value

    def get_setting(self, key: str, default: str = "") -> str:
        if key == "paper_reconcile_ok":
            return self._value
        return self._inner.get_setting(key, default)

    def __getattr__(self, item: str):
        return getattr(self._inner, item)


class _RuntimeDouble:
    """Runtime double exposing a DB whose paper_reconcile_ok can be forced."""

    def __init__(self, db, rec_value: str) -> None:
        self.db = _ForcedRecDB(db, rec_value)
        self.kill = _FakeKillFull()
        self.broker = type("B", (), {"positions": {}})()
        self.realized_equity = 100000.0
        self.trades_today = 0
        self.daily_realized_pnl = 0.0


def test_reconciliation_not_ready_blocks_ai(tmp_path):
    """§5: reconcile FAILED → AI is never called → typed REJECT with
    RECONCILIATION_NOT_READY, and no paper order."""
    from backend.paper.market_scan_loop import PaperMarketScanner
    from backend.tests.test_market_scan_loop import FakeMarketData, _atm_chain, _bars_for_ce_signal

    now = datetime(2026, 9, 18, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
    db = _db(str(tmp_path), "rec.db")
    provider = CountingProvider([APPROVE_JSON])

    engine = AITradingDecisionEngine(db=db, provider=provider)
    engine.settings["enabled"] = True

    candles = _bars_for_ce_signal(80)
    for i, c in enumerate(candles):
        c["timestamp"] = (now - timedelta(minutes=5 * (len(candles) - 1 - i))).isoformat()
    data = FakeMarketData(candles, _atm_chain(24050), 24050.0)

    class MockStrategy:
        name = "V8_D_PULLBACK_ATM"

        def evaluate_v8d_signal(self, **kwargs):
            sig = _signal()
            sig.symbol = kwargs.get("underlying_symbol", "NIFTY50")
            sig.indicators["underlying_spot"] = kwargs.get("spot_price", 24050.0)
            log = type("L", (), {"decision": "ACCEPTED"})()
            return sig, log

    submitted: List[Any] = []

    class SpyRuntime(_RuntimeDouble):
        def submit_entry(self, payload):
            submitted.append(payload)
            raise AssertionError("must not reach submit")

    spy = SpyRuntime(db, "0")  # reconciliation FAILED
    scanner = PaperMarketScanner(
        data=data, strategy=MockStrategy(),
        ai_engine=engine, ai_decision_pipeline_strategy="V8_D_PULLBACK_ATM",
    )
    res = scanner.scan_once(spy, now=now)
    assert res.traded is False
    assert res.reason == "AI_NO_TRADE:RECONCILIATION_NOT_READY"
    assert provider.calls == 0  # AI never called
    assert submitted == []


def test_reconciliation_unknown_is_reflected_in_context(tmp_path):
    """rec state None (never checked) reaches the AI context as UNKNOWN —
    never silently converted to OK."""
    ctx = build_market_context(
        symbol="NIFTY50", candles=_candles(), signal=_signal(), contract=_contract(),
        expiry="2026-09-24", risk=_risk(reconciliation_ok=None),
        session=SESSION, candles_fresh=True, candle_age_seconds=2.0,
    )
    assert ctx["risk"]["reconciliation_status"] == "UNKNOWN"
    assert ctx["risk"]["reconciliation_ok"] is None


def test_reconciliation_failed_reflected_in_context(tmp_path):
    ctx = build_market_context(
        symbol="NIFTY50", candles=_candles(), signal=_signal(), contract=_contract(),
        expiry="2026-09-24", risk=_risk(reconciliation_ok=False),
        session=SESSION, candles_fresh=True, candle_age_seconds=2.0,
    )
    assert ctx["risk"]["reconciliation_status"] == "FAILED"


# ── §7 snapshot hash determinism ─────────────────────────────────────────

def test_snapshot_hash_semantically_stable(tmp_path):
    """Same semantic input, different dict order → same hash."""
    sig = _signal()
    snap1 = build_ai_snapshot(
        build_market_context(symbol="NIFTY50", candles=_candles(), signal=sig,
                             contract=_contract(), expiry="2026-09-24", risk=_risk(),
                             session=SESSION, candles_fresh=True, candle_age_seconds=2.0),
        strategy="V8_D_PULLBACK_ATM")
    snap2 = build_ai_snapshot(
        build_market_context(symbol="NIFTY50", candles=_candles(), signal=sig,
                             contract=_contract(), expiry="2026-09-24", risk=_risk(),
                             session=SESSION, candles_fresh=True, candle_age_seconds=2.0),
        strategy="V8_D_PULLBACK_ATM")
    assert snapshot_hash(snap1) == snapshot_hash(snap2)


def test_snapshot_hash_changes_with_market_value(tmp_path):
    sig = _signal()
    snap1 = build_ai_snapshot(
        build_market_context(symbol="NIFTY50", candles=_candles(), signal=sig,
                             contract=_contract(), expiry="2026-09-24", risk=_risk(),
                             session=SESSION, candles_fresh=True, candle_age_seconds=2.0),
        strategy="V8_D_PULLBACK_ATM")
    changed = _signal()
    changed.entry_price = 86.0
    snap2 = build_ai_snapshot(
        build_market_context(symbol="NIFTY50", candles=_candles(), signal=changed,
                             contract=_contract(), expiry="2026-09-24", risk=_risk(),
                             session=SESSION, candles_fresh=True, candle_age_seconds=2.0),
        strategy="V8_D_PULLBACK_ATM")
    assert snapshot_hash(snap1) != snapshot_hash(snap2)


def test_snapshot_hash_changes_with_contract_and_bar(tmp_path):
    sig = _signal()
    snap1 = build_ai_snapshot(
        build_market_context(symbol="NIFTY50", candles=_candles(), signal=sig,
                             contract=_contract(), expiry="2026-09-24", risk=_risk(),
                             session=SESSION, candles_fresh=True, candle_age_seconds=2.0),
        strategy="V8_D_PULLBACK_ATM")
    other = _contract()
    other["strike"] = 24050.0
    snap2 = build_ai_snapshot(
        build_market_context(symbol="NIFTY50", candles=_candles(), signal=sig,
                             contract=other, expiry="2026-09-24", risk=_risk(),
                             session=SESSION, candles_fresh=True, candle_age_seconds=2.0),
        strategy="V8_D_PULLBACK_ATM")
    assert snapshot_hash(snap1) != snapshot_hash(snap2)

    snap3 = build_ai_snapshot(
        build_market_context(symbol="NIFTY50", candles=_candles(bar_ts="2026-09-18T05:05:00+00:00"),
                             signal=sig, contract=_contract(), expiry="2026-09-24",
                             risk=_risk(), session=SESSION, candles_fresh=True,
                             candle_age_seconds=2.0),
        strategy="V8_D_PULLBACK_ATM")
    assert snapshot_hash(snap1) != snapshot_hash(snap3)


def test_candle_age_not_in_snapshot(tmp_path):
    """§3 fix: the volatile per-tick age must not be part of the hashed
    snapshot (it would poison decision reuse)."""
    sig = _signal()
    snap1 = build_ai_snapshot(
        build_market_context(symbol="NIFTY50", candles=_candles(), signal=sig,
                             contract=_contract(), expiry="2026-09-24", risk=_risk(),
                             session=SESSION, candles_fresh=True, candle_age_seconds=2.0),
        strategy="V8_D_PULLBACK_ATM")
    snap2 = build_ai_snapshot(
        build_market_context(symbol="NIFTY50", candles=_candles(), signal=sig,
                             contract=_contract(), expiry="2026-09-24", risk=_risk(),
                             session=SESSION, candles_fresh=True, candle_age_seconds=57.0),
        strategy="V8_D_PULLBACK_ATM")
    assert "candle_age_seconds" not in snap1["context"]["data_freshness"]
    assert snapshot_hash(snap1) == snapshot_hash(snap2)


# ── §8/§25 warmup ────────────────────────────────────────────────────────

def test_warmup_failure_does_not_crash_engine(tmp_path):
    provider = CountingProvider([])
    provider.warm_up = lambda: {"warmed": False, "error": "ConnectionError"}  # type: ignore
    engine = AITradingDecisionEngine(db=_db(str(tmp_path), "w.db"), provider=provider)
    engine.settings["enabled"] = True
    threading.Thread(target=engine._warmup_worker, daemon=True).start()
    engine._warmup_started.wait(timeout=5)
    assert engine.warmup_status()["warmed"] is False
    # Engine still fully usable → fail-closed decisions, not crashes.
    d = engine.decide(signal_id="w1", signal=_signal(), contract=_contract(),
                      expiry="2026-09-24", candles=_candles(), candles_fresh=True,
                      candle_age_seconds=2.0, risk=_risk(), session=SESSION,
                      pipeline_strategy="V8_D_PULLBACK_ATM")
    assert d.decision == REJECT  # provider outputs empty → scripted REJECT
    assert d.allows_execution is False


def test_warmup_success_recorded(tmp_path):
    provider = CountingProvider([])
    provider.warm_up = lambda: {"warmed": True, "latency_ms": 123.4}  # type: ignore
    engine = AITradingDecisionEngine(db=_db(str(tmp_path), "w2.db"), provider=provider)
    engine.settings["enabled"] = True
    threading.Thread(target=engine._warmup_worker, daemon=True).start()
    engine._warmup_started.wait(timeout=5)
    st = engine.warmup_status()
    assert st["warmed"] is True and st["latency_ms"] == 123.4


def test_warmup_keepalive_present_in_payload():
    p = OllamaDecisionProvider(base_url="http://localhost:11434/v1", model="llama3.2:1b",
                               timeout_seconds=20.0)
    assert p.DEFAULT_KEEP_ALIVE  # configured (default 30m)


# ── §22/§23 restart + provider failures ──────────────────────────────────

def test_stored_decision_survives_engine_restart(tmp_path):
    """§22D→E: a new engine instance over the same DB replays the stored
    setup decision — no re-inference after restart, no duplicate approval."""
    path = os.path.join(str(tmp_path), "restart.db")
    db1 = _db(str(tmp_path), "restart.db")
    provider1 = CountingProvider([APPROVE_JSON])
    e1 = AITradingDecisionEngine(db=db1, provider=provider1)
    e1.settings["enabled"] = True
    d1 = e1.decide(signal_id="r1", signal=_signal(), contract=_contract(),
                   expiry="2026-09-24", candles=_candles(), candles_fresh=True,
                   candle_age_seconds=2.0, risk=_risk(), session=SESSION,
                   pipeline_strategy="V8_D_PULLBACK_ATM")
    # "restart": brand-new engine + fresh provider over the SAME database.
    db2 = _db(str(tmp_path), "restart.db")
    provider2 = CountingProvider([])
    e2 = AITradingDecisionEngine(db=db2, provider=provider2)
    e2.settings["enabled"] = True
    d2 = e2.decide(signal_id="r2-different-pipeline-id", signal=_signal(),
                   contract=_contract(), expiry="2026-09-24", candles=_candles(),
                   candles_fresh=True, candle_age_seconds=9.0, risk=_risk(),
                   session=SESSION, pipeline_strategy="V8_D_PULLBACK_ATM")
    assert provider2.calls == 0
    assert d2.decision_id == d1.decision_id
    assert d2.decision == APPROVE


def test_provider_500_classified(tmp_path):
    import urllib.error
    provider = CountingProvider([
        urllib.error.HTTPError("http://x", 500, "boom", None, None)])
    engine = AITradingDecisionEngine(db=None, provider=provider)
    d = engine.decide(signal_id="f1", signal=_signal(), contract=_contract(),
                      expiry="2026-09-24", candles=_candles(), candles_fresh=True,
                      candle_age_seconds=2.0, risk=_risk(), session=SESSION,
                      pipeline_strategy="V8_D_PULLBACK_ATM")
    assert d.decision == REJECT
    assert d.reason_codes and d.reason_codes[0] in ("AI_PROVIDER_UNAVAILABLE", "AI_INVALID_RESPONSE")


def test_provider_connection_reset(tmp_path):
    provider = CountingProvider([ConnectionResetError("reset by peer")])
    engine = AITradingDecisionEngine(db=None, provider=provider)
    d = engine.decide(signal_id="f2", signal=_signal(), contract=_contract(),
                      expiry="2026-09-24", candles=_candles(), candles_fresh=True,
                      candle_age_seconds=2.0, risk=_risk(), session=SESSION,
                      pipeline_strategy="V8_D_PULLBACK_ATM")
    assert d.decision == REJECT
    assert "AI_PROVIDER_UNAVAILABLE" in d.reason_codes


def test_latency_stats_track_timeouts(tmp_path):
    db = _db(str(tmp_path), "lat.db")
    store = AIDecisionStore(db)
    for i in range(3):
        store.record_latency(provider="ollama:ollama", model="llama3.2:1b",
                             latency_ms=3800.0, timeout_seconds=20.0, success=True)
    store.record_latency(provider="ollama:ollama", model="llama3.2:1b",
                         latency_ms=20050.0, timeout_seconds=20.0,
                         success=False, error_code="AI_TIMEOUT")
    stats = store.latency_stats()
    assert stats["samples"] == 4
    assert stats["success_count"] == 3
    assert stats["error_counts"]["AI_TIMEOUT"] == 1
    assert stats["latency_ms_p95"] >= stats["latency_ms_median"]
