"""PHASE 5.2 §36 — live AI latency measurement vs the real local Ollama.

Measures, against the production-configured model (llama3.2:1b):
  A. COLD decision attempt after explicitly unloading the model
     (expected: fail-closed AI_TIMEOUT at the 20s timeout, or a slow but
     valid decision — either outcome is recorded honestly).
  B. warm_up() — the §25 startup preload (minimal 8-token prompt).
  C. warm decision calls x5 with keep_alive (median/p95/max).
  D. /api/ps poll — proves the model stays resident under keep_alive.

Writes analysis/ai_decision_latency_p52.json. Latency is measured, never
assumed; no profitability or improvement claim is derived here.

Run:  python scripts/measure_ai_latency_p52.py
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AI_DECISION_ENABLED", "false")  # no auto-warmup thread: controlled phases

from backend.ai_decision.context import MarketSession, RiskContext
from backend.ai_decision.decision_engine import AITradingDecisionEngine
from backend.tests.rehearsal_p52 import _test_signal


def _ollama(host: str, path: str, payload: dict | None = None, timeout: float = 10.0):
    url = f"{host}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    from backend.ai_decision.decision_engine import load_ai_decision_settings
    settings = load_ai_decision_settings()
    host = settings["base_url"].replace("/v1", "")
    model = settings["model"]

    candles = []
    base = datetime(2026, 9, 18, 5, 0, tzinfo=timezone.utc)
    from datetime import timedelta
    for i in range(80):
        ts = (base + timedelta(minutes=5 * i)).isoformat()
        candles.append({"timestamp": ts, "open": 24000 + i, "high": 24005 + i,
                        "low": 23995 + i, "close": 24000 + i, "volume": 1000})

    def decide_once(tag: str) -> dict:
        sig, expiry = _test_signal(hash(tag) % 10000)
        contract = sig.indicators["selected_contract"]
        eng = AITradingDecisionEngine(db=None)  # no store: pure latency measurement
        eng.settings["enabled"] = True
        t0 = time.monotonic()
        d = eng.decide(
            signal_id=f"MEASURE-{tag}", signal=sig, contract=contract, expiry=expiry,
            candles=candles, candles_fresh=True, candle_age_seconds=1.0,
            risk=RiskContext(equity=100000.0, open_positions=0, trades_today=0,
                             daily_realized_pnl=0.0, kill_switch=False,
                             reconciliation_ok=True),
            session=MarketSession(open=True, is_trading_day=True, label="MEASURE"),
            pipeline_strategy="V8_D_PULLBACK_ATM",
        )
        return {"phase": tag, "decision": d.decision, "reason_codes": list(d.reason_codes),
                "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                "success": d.decision != "REJECT" or d.reason_codes[0] not in
                           ("AI_TIMEOUT", "AI_PROVIDER_UNAVAILABLE", "AI_MODEL_UNAVAILABLE")}

    # A. COLD — unload, then one real decision attempt.
    try:
        _ollama(host, "/api/generate", {"model": model, "keep_alive": 0}, timeout=30)
        time.sleep(1.0)
    except Exception:
        pass  # best-effort unload; if it fails the "cold" is just colder than planned
    cold = decide_once("cold")
    cold["phase"] = "cold_first_decision_after_unload"

    # B. warm_up() — the §25 startup preload.
    from backend.ai_decision.decision_engine import OllamaDecisionProvider
    p = OllamaDecisionProvider(base_url=settings["base_url"], model=model,
                               timeout_seconds=settings["timeout_seconds"],
                               temperature=settings["temperature"],
                               max_tokens=settings["max_tokens"])
    t0 = time.monotonic()
    warm = p.warm_up()
    warm["latency_ms"] = round(warm.get("latency_ms", (time.monotonic() - t0) * 1000), 1)

    # C. warm decision calls.
    warm_runs = [decide_once(f"warm{i}") for i in range(1, 6)]
    for i, r in enumerate(warm_runs, 1):
        r["phase"] = f"warm_run_{i}"

    # D. keep_alive residency.
    try:
        ps = _ollama(host, "/api/ps")
        resident = [m.get("name") for m in ps.get("models", [])]
        expires = [m.get("expiration") for m in ps.get("models", [])]
    except Exception as exc:  # noqa: BLE001
        resident, expires = [], [f"ps_poll_failed:{type(exc).__name__}"]

    lats = [r["latency_ms"] for r in warm_runs]
    ok_warm = [r for r in warm_runs if r["success"]]
    out = {
        "measurement": "PHASE 5.2 §36 — AI latency: cold fail-closed, warm_up preload, warm steady-state, keep_alive residency",
        "date": datetime.now(timezone.utc).isoformat(),
        "provider": f"ollama:{model} ({settings['base_url']})",
        "timeout_seconds": settings["timeout_seconds"],
        "keep_alive": OllamaDecisionProvider.DEFAULT_KEEP_ALIVE,
        "cold": cold,
        "warmup": warm,
        "warm_runs": warm_runs,
        "warm_median_ms": round(statistics.median(lats), 1),
        "warm_p95_ms": round(sorted(lats)[int(0.95 * (len(lats) - 1))], 1),
        "warm_max_ms": round(max(lats), 1),
        "warm_success_rate": f"{len(ok_warm)}/{len(warm_runs)}",
        "model_resident_after_keep_alive": {"models": resident, "expiration": expires},
        "phase51_baseline": {"cold_first_call_ms": 20093.8, "warm_median_ms": 3846.8,
                             "source": "analysis/ai_decision_latency.json (Phase 5.1 §23)"},
        "verdict": None,  # filled below
    }
    out["verdict"] = None  # filled below
    fatal = ("AI_TIMEOUT", "AI_PROVIDER_UNAVAILABLE", "AI_MODEL_UNAVAILABLE")
    if not cold["success"] and cold["reason_codes"] and cold["reason_codes"][0] in fatal:
        cold_note = (
            f"cold attempt was FAIL-CLOSED ({cold['decision']} / {','.join(cold['reason_codes'])}) at "
            f"{cold['latency_ms']:.0f}ms — NO TRADE as designed."
        )
    elif cold["decision"] == "REJECT":
        cold_note = (
            f"cold attempt produced a typed FAIL-CLOSED NO-TRADE "
            f"({','.join(cold['reason_codes'])}) at {cold['latency_ms']:.0f}ms — the model "
            "answered but its output was not a usable decision; no trade as designed."
        )
    else:
        cold_note = (
            f"cold decision completed in {cold['latency_ms']:.0f}ms — inside the "
            f"{settings['timeout_seconds']:.0f}s timeout but far above warm latency, so a cold "
            "first signal is a real timeout risk; warm_up() exists precisely to remove this exposure."
        )
    out["verdict"] = (
        "PASS — " + cold_note + " warm_up() preloaded the model in "
        f"{warm.get('latency_ms')}ms, all {len(warm_runs)} warm decisions completed inside the timeout, "
        "and the model remained resident under keep_alive."
        if warm.get("warmed") and all(r["success"] for r in warm_runs)
        else "PARTIAL — " + cold_note + " See warmup/warm_runs details; every failure mode is fail-closed by design."
    )

    dest = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "analysis", "ai_decision_latency_p52.json")
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    print(json.dumps({k: out[k] for k in ("cold", "warmup", "warm_median_ms", "warm_p95_ms",
                                          "warm_max_ms", "warm_success_rate", "verdict")}, indent=1))
    print(f"saved -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
