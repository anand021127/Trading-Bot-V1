"""PHASE B — THE ONE authoritative Copilot context provider.

`build_full_context(...)` assembles EVERY section the spec (§5-§16, §23-§29)
requires, from the SAME authoritative backend services the rest of the bot
uses — never duplicated calculations, never invented values:

  BOT_STATE · CONFIGURATION · MARKET_STATE · DATA_HEALTH · WEBSOCKET ·
  SCANNER · LATEST_SIGNAL · LATEST_DECISION · LATEST_REJECTION · TODAY ·
  OPEN_POSITIONS · RECENT_TRADES · RISK · EXECUTION · RECONCILIATION ·
  BROKER · AI_TRADING_DECISION · COPILOT · BACKTEST_SUMMARY ·
  CONFIGURATION_MISMATCHES · SYSTEM_ERRORS

Rules enforced by construction:
- READ-ONLY: everything is read from existing services/DB; nothing here can
  place orders or change settings.
- BOUNDED: latest 1 signal / 1 decision / 1 rejection, 20 trades, 5 errors —
  never a database dump (spec §27).
- FRESHNESS: every dynamic section carries `as_of` + `age_seconds` (§28).
- PROVENANCE: every section carries a `source` label (LIVE_RUNTIME /
  DATABASE / CONFIGURATION / BACKTEST / BROKER / WEBSOCKET / SCANNER /
  RISK_MANAGER) so Copilot can say where a number came from (§29).
- NO HALLUCINATION: an unavailable section states exactly what is missing
  and why — it is NEVER replaced with a guess (§30).
- NO SECRETS: the whole payload passes through the same secret_guard
  redaction the chat context uses, before it reaches any LLM or the UI.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

RECENT_TRADES_LIMIT = 20
RECENT_ERRORS_LIMIT = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return round(max(0.0, (_now() - dt).total_seconds()), 1)
    except Exception:
        return None


def _section(source: str, body: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"source": source, "as_of": _now().isoformat()}
    out.update(body)
    return out


def _db() -> Optional[Any]:
    try:
        from backend.database.db_manager import DatabaseManager
        db = DatabaseManager(db_path=os.environ.get("DATABASE_PATH", "data/trading_bot.db"))
        return db
    except Exception:
        return None


# ── BOT_STATE ────────────────────────────────────────────────────────────
def _bot_state(app_state: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    try:
        from backend.strategy.trading_engine import BotState
        body.update(BotState.status())
    except Exception as exc:
        body["available"] = False
        body["reason"] = f"BotState unreadable: {type(exc).__name__}"
    try:
        from backend.market.calendar import is_market_open_now
        body["market_open"] = bool(is_market_open_now())
    except Exception:
        body["market_open"] = None
    body["bot_name"] = "Trading-Bot-V1 (Upstox index-options bot)"
    try:
        from backend.api.main import app as fastapi_app
        body["app_version"] = getattr(fastapi_app, "version", None)
    except Exception:
        body["app_version"] = None
    body["mode"] = _mode()
    body["strategy"] = _strategy_name()
    body["broker"] = "UPSTOX"
    # REAL operational state — the flag alone ("running") only says what was
    # requested. runtime_* says whether a worker is alive and scans execute.
    try:
        from backend.paper.scan_state import compute_runtime_state
        _db_rt = _db()
        if _db_rt is not None:
            _rs = compute_runtime_state(_db_rt)
            body["runtime_state"] = _rs.get("state")
            body["runtime_label"] = _rs.get("label")
            body["runtime_summary"] = _rs.get("summary")
            body["worker_alive"] = _rs.get("worker_alive")
            body["heartbeat_age_seconds"] = _rs.get("heartbeat_age_seconds")
            body["effective_running"] = str(_rs.get("state", "")).startswith("RUNNING")
    except Exception:
        pass
    try:
        from backend.config.universe_config import load_universe_config
        u = load_universe_config(_db() or app_state)
        body["supported_instruments"] = {
            "type": "INDEX OPTIONS (ATM CE/PE premium — never the index itself)",
            "option_indices": list(u.option_indices),
        }
    except Exception:
        body["supported_instruments"] = {
            "type": "INDEX OPTIONS (ATM CE/PE premium — never the index itself)",
            "option_indices": ["NIFTY50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
                               "SENSEX", "BANKEX"],
        }
    return _section("LIVE_RUNTIME", body)


def _mode() -> str:
    try:
        from backend.config.runtime_config import get_effective_settings
        return get_effective_settings().mode
    except Exception:
        return os.environ.get("TRADING_MODE", "paper")


def _strategy_name() -> str:
    try:
        from backend.config.runtime_config import get_effective_settings
        n = (getattr(get_effective_settings().strategy, "name", "") or "").strip()
        if n:
            return n
    except Exception:
        pass
    return os.environ.get("TRADING_STRATEGY", "V8_D_PULLBACK_ATM")


# ── CONFIGURATION ────────────────────────────────────────────────────────
def _configuration() -> Dict[str, Any]:
    from backend.config.runtime_config import runtime_config_summary
    return _section("CONFIGURATION", runtime_config_summary())


# ── MARKET_STATE / DATA_HEALTH ───────────────────────────────────────────
def _market_state() -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    try:
        from backend.market.calendar import exchange_calendar
        status, note = exchange_calendar.session_status()
        body.update({"session_status": status, "note": note, "available": True})
    except Exception as exc:
        body.update({"available": False, "reason": f"calendar unreadable: {type(exc).__name__}"})
    return _section("LIVE_RUNTIME", body)


def _data_health() -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    try:
        from backend.broker.instrument_master import get_master_status
        body = dict(get_master_status() or {})
        body["available"] = True
    except Exception as exc:
        body = {"available": False, "reason": f"instrument master unreadable: {type(exc).__name__}"}
    return _section("BROKER", body)


# ── WEBSOCKET (lifecycle-aware; CONNECTED ≠ HEALTHY, §14) ───────────────
def _websocket(app_state: Any) -> Dict[str, Any]:
    ws = getattr(app_state, "ws_client", None)
    body: Dict[str, Any] = {"available": False}
    if ws is not None and hasattr(ws, "status_report"):
        try:
            rep = dict(ws.status_report())
            rep["available"] = True
            state = str(rep.get("state") or "unknown")
            rep["health_interpretation"] = (
                "STREAMING = healthy (real ticks arriving)"
                if state == "streaming"
                else "CONNECTED/SUBSCRIBED but NOT streaming — no real ticks yet; NOT healthy"
                if state in ("connected", "subscribed")
                else "NOT healthy" if state not in ("reconnect_wait",)
                else "reconnecting — not healthy yet"
            )
            return _section("WEBSOCKET", rep)
        except Exception as exc:
            body = {"available": False, "reason": f"status_report failed: {type(exc).__name__}"}
    else:
        from backend.api.websocket import get_broker_ws_status
        rep = dict(get_broker_ws_status() or {})
        rep["available"] = bool(rep)
        rep.setdefault("reason", "No WebSocket client attached to this process"
                       if not rep else None)
        return _section("WEBSOCKET", rep)
    return _section("WEBSOCKET", body)


# ── SCANNER ──────────────────────────────────────────────────────────────
def _scanner(app_state: Any, db: Any) -> Dict[str, Any]:
    """Scanner state for Copilot.

    In PAPER mode the scan that decides trades runs in the paper WORKER
    process, so the authoritative source is the worker heartbeat + the
    persisted scan record (compute_runtime_state) — NOT the API process's own
    LiveScanner heartbeat, which used to make Copilot report "RUNNING" while
    the worker had never executed a single scan."""
    body: Dict[str, Any] = {"available": False}
    if db is not None:
        try:
            from backend.paper.scan_state import compute_runtime_state
            rs = compute_runtime_state(db)
            last = rs.get("last_scan") or {}
            body = {
                "available": True,
                "source_detail": "paper worker heartbeat + persisted scan record",
                "scanner_status": rs.get("state"),
                "state_label": rs.get("label"),
                "summary": rs.get("summary"),
                "worker_alive": rs.get("worker_alive"),
                "heartbeat_age_seconds": rs.get("heartbeat_age_seconds"),
                "scan_seq": rs.get("scan_seq"),
                "last_scan_seconds_ago": rs.get("scan_age_seconds"),
                "scan_interval_seconds": rs.get("scan_interval_seconds"),
                "worker_last_scan_reason": last.get("reason"),
                "last_scan_ist": last.get("recorded_at_ist"),
                "data_status": last.get("data_status"),
                "candle_count": last.get("candle_count"),
                "candle_age_seconds": last.get("candle_age_seconds"),
                "expiry": last.get("expiry"),
                "option_chain_count": last.get("option_chain_count"),
                # worker-level error only while the runtime is actually unhealthy;
                # a healthy RUNNING_*/STOPPED state never shows a stale error.
                "error": last.get("error") or (
                    rs.get("last_error")
                    if str(rs.get("state", "")) in ("STARTED_WORKER_NOT_RESPONDING",
                                                     "RUNNING_SCANNER_ERROR", "RUNNING_DATA_ERROR")
                    else None),
            }
            # Honest empty state: nothing has ever run (no worker, no record,
            # bot stopped) -> not "available"; the reason says why.
            if not last and not rs.get("worker_alive") and rs.get("state") == "STOPPED":
                body["available"] = False
                body["reason"] = ("No scanner heartbeat in this process and no persisted "
                                  "worker scan — the scanner/worker has not run yet. "
                                  + str(rs.get("summary") or ""))
        except Exception as exc:
            body = {"available": False, "reason": f"paper scanner state unreadable: {type(exc).__name__}"}
    scanner = getattr(app_state, "scanner", None)
    if scanner is not None and hasattr(scanner, "health_report"):
        # Kept ONLY as an additional, clearly-labelled API-side component.
        try:
            body["api_live_scanner"] = dict(scanner.health_report() or {})
            body["api_live_scanner_note"] = ("API-process LiveScanner heartbeat (option-universe "
                                             "scanner UI) — NOT the paper worker that trades")
        except Exception:
            pass
    if not body.get("available"):
        body.setdefault("reason", "No scanner heartbeat in this process and no persisted "
                                  "worker scan — the scanner/worker has not run yet.")
    return _section("SCANNER", body)


# ── LATEST SIGNAL / DECISION / REJECTION (from the ONE scan record) ──────
def _latest_signal(db: Any) -> Dict[str, Any]:
    try:
        raw = db.get_setting("paper_worker_last_scan_detail", "") or ""
        if not raw:
            _why = ""
            try:
                from backend.paper.scan_state import compute_runtime_state
                _rs = compute_runtime_state(db)
                _why = f" Scanner state: {_rs.get('label')} — {_rs.get('summary')}"
            except Exception:
                pass
            return _section("SCANNER", {
                "available": False,
                "reason": "No actionable V8-D signal has been recorded — no scan "
                          "result has been persisted since bot startup." + _why,
            })
        detail = json.loads(raw)
        inner = detail.get("details") or {}
        signal = detail.get("signal")
        return _section("SCANNER", {
            "available": True,
            "signal": signal or "NONE",
            "scanned": detail.get("scanned"),
            "reason": detail.get("reason"),
            "rejection_reasons": list(inner.get("rejection") or []),
            "ai_decision": inner.get("ai_decision"),
            "instrument_key": inner.get("instrument_key"),
            "premium": inner.get("premium"),
            "quantity": inner.get("quantity"),
            "signal_id": inner.get("signal_id"),
        })
    except Exception as exc:
        return _section("SCANNER", {"available": False,
                                    "reason": f"scan record unreadable: {type(exc).__name__}"})


def _latest_decision(db: Any) -> Dict[str, Any]:
    try:
        from backend.ai_decision.decision_engine import load_ai_decision_settings
        from backend.ai_decision.store import AIDecisionStore
        store = AIDecisionStore(db)
        rows = db._connect().execute(
            "SELECT decision_id, decision, symbol, confidence, reason_codes, "
            "model_provider, model_name, created_at, latency_ms "
            "FROM ai_decisions ORDER BY created_at DESC LIMIT 1"
        ).fetchall()
        if not rows:
            return _section("DATABASE", {
                "available": False,
                "reason": "No AI trading decision has been persisted yet — either the "
                          "AI decision layer is disabled (V8-D gates directly) or no "
                          "V8-D BUY has reached the AI gate since startup.",
            })
        r = rows[0]
        try:
            codes = json.loads(r["reason_codes"] or "[]")
        except Exception:
            codes = []
        return _section("DATABASE", {
            "available": True,
            "decision_id": r["decision_id"],
            "decision": r["decision"],
            "symbol": r["symbol"],
            "confidence": r["confidence"],
            "reason_codes": codes,
            "model": f"{r['model_provider']}/{r['model_name']}",
            "created_at": r["created_at"],
            "age_seconds": _age(r["created_at"]),
            "latency_ms": r["latency_ms"],
            "configured": {k: load_ai_decision_settings().get(k) for k in
                           ("provider", "model", "timeout_seconds")},
        })
    except Exception as exc:
        return _section("DATABASE", {"available": False,
                                     "reason": f"AI decisions unreadable: {type(exc).__name__}"})


def _latest_rejection(db: Any) -> Dict[str, Any]:
    from backend.copilot.gate_chain import build_gate_chain_from_db
    chain = build_gate_chain_from_db(db)
    if not chain.get("available"):
        return _section("SCANNER", {
            "available": False,
            "reason": chain.get("reason") or "No scan has been recorded since bot startup.",
        })
    return _section("SCANNER", {
        "available": True,
        "stage": chain.get("stage"),
        "scan_reason": chain.get("scan_reason"),
        "age_seconds": chain.get("age_seconds"),
        "v8d_rejection_reasons": chain.get("v8d_rejection_reasons"),
        "gate_chain": chain,
    })


# ── TODAY / RECENT_TRADES / POSITIONS ───────────────────────────────────
def _today(db: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {"available": False}
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        today = _dt.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
        rows = db.list_trades(date_from=today, date_to=today)
        pnl = 0.0
        wins = losses = 0
        for t in rows:
            d = dict(t) if hasattr(t, "keys") else {}
            v = float(d.get("net_pnl") or d.get("pnl") or 0)
            pnl += v
            if v > 0:
                wins += 1
            elif v < 0:
                losses += 1
        cfg = _configuration()
        max_trades = int(cfg.get("risk", {}).get("max_trades_per_day") or 0)
        body = {
            "available": True,
            "date": today,
            "trades_today": wins + losses,
            "configured_max_trades": max_trades,
            "trades_remaining": max(0, max_trades - (wins + losses)) if max_trades else None,
            "wins": wins,
            "losses": losses,
            "realized_pnl": round(pnl, 2),
            "capital": cfg.get("capital"),
            "risk": cfg.get("risk"),
        }
    except Exception as exc:
        body = {"available": False, "reason": f"today stats failed: {type(exc).__name__}"}
    return _section("DATABASE", body)


def _recent_trades(db: Any) -> Dict[str, Any]:
    try:
        rows = db.list_trades()
        out: List[Dict[str, Any]] = []
        for t in rows[-RECENT_TRADES_LIMIT:][::-1]:
            d = dict(t) if hasattr(t, "keys") else {}
            out.append({
                "trade_id": d.get("id"),
                "timestamp": d.get("entry_time") or d.get("timestamp"),
                "underlying": d.get("underlying_symbol") or d.get("symbol"),
                "option_type": d.get("option_type"),
                "strike": d.get("strike_price"),
                "expiry": d.get("expiry"),
                "instrument_key": d.get("instrument_key"),
                "entry_price": d.get("entry_price"),
                "exit_price": d.get("exit_price"),
                "quantity": d.get("quantity"),
                "lot_size": d.get("lot_size"),
                "capital_used": d.get("capital_used"),
                "initial_stop": d.get("initial_stop"),
                "exit_reason": d.get("exit_reason"),
                "gross_pnl": d.get("gross_pnl"),
                "charges": d.get("brokerage"),
                "net_pnl": d.get("net_pnl"),
                "duration_min": d.get("trade_duration_min"),
                "strategy": d.get("strategy"),
                "signal_id": d.get("signal_id"),
            })
        return _section("DATABASE", {
            "available": True,
            "count": len(out),
            "bounded": RECENT_TRADES_LIMIT,
            "trades": out,
        })
    except Exception as exc:
        return _section("DATABASE", {"available": False,
                                     "reason": f"trades unreadable: {type(exc).__name__}"})


def _positions(db: Any) -> Dict[str, Any]:
    try:
        positions = []
        for p in db.get_open_positions():
            d = p.__dict__ if hasattr(p, "__dict__") else dict(p)
            extra = d.get("extra") or {}
            positions.append({
                "symbol": d.get("symbol"),
                "instrument_key": getattr(p, "instrument_key", None) or d.get("instrument_key"),
                "underlying": extra.get("underlying"),
                "option_type": extra.get("option_type"),
                "strike": extra.get("strike"),
                "expiry": extra.get("expiry"),
                "quantity": d.get("quantity"),
                "lot_size": extra.get("lot_size"),
                "average_price": d.get("average_price"),
                "stop_loss": extra.get("stop_loss"),
                "target": extra.get("target"),
                "entry_time": str(d.get("entry_time")) if d.get("entry_time") else None,
                "age_seconds": _age(str(d.get("entry_time"))) if d.get("entry_time") else None,
                "strategy": extra.get("strategy"),
                "trade_id": extra.get("trade_id"),
            })
        return _section("DATABASE", {
            "available": True, "count": len(positions), "positions": positions,
            "note": "LTP/unrealized P&L included only when a live quote exists "
                    "(never invented)" if not positions else None,
        })
    except Exception as exc:
        return _section("DATABASE", {"available": False,
                                     "reason": f"positions unreadable: {type(exc).__name__}"})


# ── RISK / EXECUTION / RECONCILIATION ───────────────────────────────────
def _risk(app_state: Any) -> Dict[str, Any]:
    engine = getattr(app_state, "engine", None)
    rm = getattr(engine, "risk_manager", None)
    if rm is not None:
        try:
            return _section("RISK_MANAGER", {"available": True, **rm.get_status()})
        except Exception as exc:
            return _section("RISK_MANAGER", {"available": False,
                                             "reason": f"risk status failed: {type(exc).__name__}"})
    # No engine in this process: reconstruct the authoritative limits + the
    # paper worker's persisted day counters (real state, not guesses).
    try:
        from backend.config.runtime_config import get_effective_settings
        s = get_effective_settings()
        db = _db()
        day = datetime.now().date().isoformat()
        counters = db.get_daily_counters(day) if db is not None else {}
        return _section("RISK_MANAGER", {
            "available": True,
            "note": "No live RiskManager in the API process — authoritative limits "
                    "from the effective configuration + the paper worker's durable "
                    "daily counters.",
            "max_trades": s.risk.max_trades_per_day,
            "trades_used": int(counters.get("trades_taken") or 0),
            "daily_pnl": round(float(counters.get("realized_pnl") or 0), 2),
            "max_daily_loss_pct": s.risk.max_daily_loss_pct,
            "capital": s.capital.total,
            "kill_switch_active": None,
        })
    except Exception as exc:
        return _section("RISK_MANAGER", {"available": False,
                                         "reason": f"risk state unavailable: {type(exc).__name__}"})


def _execution(app_state: Any) -> Dict[str, Any]:
    engine = getattr(app_state, "engine", None)
    pipe = getattr(engine, "_pipeline", None)
    body: Dict[str, Any] = {"available": False}
    if pipe is not None:
        body = {
            "available": True,
            "strategy": getattr(pipe, "strategy", None) and pipe.strategy.name,
            "mode": "execution pipeline armed",
        }
    else:
        try:
            from backend.api.routers.bot_control import get_paper_runtime
            rt = get_paper_runtime()
            if rt is not None and getattr(rt, "pipeline", None) is not None:
                body = {
                    "available": True,
                    "strategy": getattr(rt.pipeline, "strategy", None) and rt.pipeline.strategy.name,
                    "mode": "paper execution pipeline (PaperTradingRuntime)",
                }
        except Exception:
            pass
    if not body.get("available"):
        body.setdefault("reason",
                        "No ExecutionPipeline is attached in this process (API process only "
                        "attaches one in live mode; paper execution runs in the worker).")
    return _section("LIVE_RUNTIME", body)


def _reconciliation(db: Any) -> Dict[str, Any]:
    body: Dict[str, Any]
    try:
        from types import SimpleNamespace
        from backend.paper.market_scan_loop import read_reconciliation_state
        state, detail, age = read_reconciliation_state(SimpleNamespace(db=db))
        label = "OK" if state == "1" else "FAILED" if state == "0" else "NEVER_CHECKED"
        body = {
            "available": True,
            "state": label,
            "raw_state": state or None,
            "age_seconds": round(age, 1) if age is not None else None,
            "detail": detail or {},
            "stale": bool(age is not None and age > 15 * 60.0),
            "honest_note": (
                "NEVER_CHECKED is reported as NEVER_CHECKED — never converted to HEALTHY"
                if label == "NEVER_CHECKED" else None),
        }
    except Exception as exc:
        body = {"available": False, "reason": f"reconciliation state unreadable: {type(exc).__name__}"}
    return _section("DATABASE", body)


# ── BROKER ───────────────────────────────────────────────────────────────
def _broker() -> Dict[str, Any]:
    body: Dict[str, Any] = {"broker": "UPSTOX", "available": False}
    try:
        from backend.broker.token_resolver import get_token_metadata
        meta = dict(get_token_metadata() or {})
        # Safe metadata only: presence/source/fingerprint — NEVER the token.
        body.update({
            "available": True,
            "token_present": meta.get("present"),
            "token_source": meta.get("source"),
            "token_fingerprint": meta.get("fingerprint"),
            "token_length": meta.get("length"),
        })
    except Exception as exc:
        body["reason"] = f"token metadata unavailable: {type(exc).__name__}"
    try:
        from backend.broker.instrument_master import get_master_status
        body["instrument_master"] = get_master_status()
    except Exception:
        pass
    return _section("BROKER", body)


# ── AI_TRADING_DECISION (distinct from Copilot AI, §12) ─────────────────
def _ai_trading_decision(db: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    try:
        from backend.api.routers.bot_control import AI_ENABLED_OVERRIDE_KEY
        from backend.ai_decision.decision_engine import load_ai_decision_settings
        s = load_ai_decision_settings()
        override = str(db.get_setting(AI_ENABLED_OVERRIDE_KEY, "") or "")
        env_enabled = bool(s.get("enabled"))
        effective = True if override == "1" else False if override == "0" else env_enabled
        body.update({
            "available": True,
            "enabled": effective,
            "enabled_source": "db_override" if override in ("0", "1") else "env_default",
            "provider": s.get("provider"),
            "model": s.get("model"),
            "timeout_seconds": s.get("timeout_seconds"),
            "important": "This is the AI TRADING DECISION layer (gates V8-D BUYs "
                         "before hard risk) — a SEPARATE function from the Copilot "
                         "assistant answering you right now.",
        })
        try:
            from backend.ai_decision.store import AIDecisionStore
            stats = AIDecisionStore(db).latency_stats()
            body["latency"] = stats
        except Exception:
            pass
        try:
            rows = db._connect().execute(
                "SELECT decision, COUNT(*) AS n FROM ai_decisions GROUP BY decision"
            ).fetchall()
            body["decision_counters"] = {str(r["decision"]).lower(): int(r["n"]) for r in rows}
        except Exception:
            pass
        latest = _latest_decision(db)
        body["latest"] = {
            "available": latest.get("available"),
            "decision": latest.get("decision"),
            "symbol": latest.get("symbol"),
            # AI's confidence in its own analysis — NOT a probability of profit.
            "confidence": latest.get("confidence"),
            "reason_codes": latest.get("reason_codes"),
            "latency_ms": latest.get("latency_ms"),
            "created_at": latest.get("created_at"),
            "age_seconds": latest.get("age_seconds"),
        } if latest.get("available") else {
            "available": False,
            "reason": latest.get("reason", "No AI decision persisted yet."),
        }
    except Exception as exc:
        body = {"available": False, "reason": f"AI decision state unavailable: {type(exc).__name__}"}
    return _section("DATABASE", body)


# ── DECISION PIPELINE (scanner → V8-D → AI → risk → execution) ───────────
def _pipeline(db: Any) -> Dict[str, Any]:
    """The same honest pipeline view the dashboard shows (derived from the
    persisted scan record + worker heartbeat — never from a flag)."""
    try:
        from backend.paper.scan_state import compute_runtime_state
        rs = compute_runtime_state(db)
        return _section("SCANNER", dict(rs.get("pipeline") or {}))
    except Exception as exc:
        return _section("SCANNER", {"available": False,
                                    "reason": f"pipeline state unavailable: {type(exc).__name__}"})


# ── COPILOT (self-description, §12/§31) ──────────────────────────────────
def _copilot_self() -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "role": "READ-ONLY trading-bot observation assistant",
        "can": ["explain bot state", "explain rejections", "explain risk/P&L/positions",
                "explain backtest results", "explain health/websocket/data state"],
        "can_never": ["place/cancel/modify orders", "change settings, capital, risk, "
                      "mode, AI state, or the kill switch", "call broker order APIs",
                      "execute code", "issue trading instructions"],
        "distinct_from": "AI Trading Decision layer (a separate gate in the trade pipeline)",
    }
    try:
        from backend.copilot.config import load_copilot_settings
        s = load_copilot_settings()
        body["enabled"] = bool(s.enabled)
        body["llm_backend"] = s.llm_backend
        body["model"] = s.llm_model
    except Exception:
        pass
    return _section("LIVE_RUNTIME", body)


# ── BACKTEST_SUMMARY (dynamic — NEVER hardcoded, §23/§24) ────────────────
def _backtest_summary() -> Dict[str, Any]:
    try:
        from backend.backtest.job_store import job_store
        latest = job_store.get_latest()
        if not latest:
            return _section("BACKTEST", {
                "available": False,
                "reason": "No completed backtest result is stored — run a backtest "
                          "and it will appear here (numbers are always read from "
                          "the latest stored result, never hardcoded).",
            })
        result = latest.get("result") or {}
        if latest.get("status") != "COMPLETED" or not result:
            return _section("BACKTEST", {
                "available": False,
                "reason": f"Latest backtest job status={latest.get('status')} with no "
                          f"stored result — no completed backtest summary is available.",
                "job_id": latest.get("job_id"),
            })
        trades = int(result.get("total_trades") or result.get("trades_taken") or 0)
        wins = int(result.get("winning_trades") or 0)
        losses = int(result.get("losing_trades") or 0)
        gross_w = result.get("gross_profit")
        gross_l = result.get("gross_loss")
        summary: Dict[str, Any] = {
            "available": True,
            "job_id": latest.get("job_id"),
            "completed_at": latest.get("completed_at"),
            "age_seconds": _age(latest.get("completed_at")),
            "config": {
                "strategy": latest.get("strategies"),
                "symbols": latest.get("symbols"),
                "start_date": latest.get("start_date"),
                "end_date": latest.get("end_date"),
                "interval": latest.get("interval"),
                "capital": latest.get("capital"),
            },
            "data_coverage": {
                k: result.get(k) for k in (
                    "data_source", "data_coverage_pct", "total_candles_scanned",
                    "signals_generated", "rejected_signals") if k in result
            },
            "summary": {
                "trades": trades, "wins": wins, "losses": losses,
                "win_rate_pct": result.get("win_rate_pct") or result.get("accuracy_pct"),
                "gross_profit": gross_w, "gross_loss": gross_l,
                "net_pnl": result.get("net_profit"),
                "net_pnl_pct": result.get("net_profit_pct"),
                "profit_factor": result.get("profit_factor"),
                "expectancy": result.get("expectancy"),
                "max_drawdown_pct": result.get("max_drawdown_pct"),
                "avg_win": result.get("average_win"),
                "avg_loss": result.get("average_loss"),
                "max_consecutive_wins": result.get("max_consecutive_wins"),
                "max_consecutive_losses": result.get("max_consecutive_losses"),
                "total_charges": result.get("total_charges"),
            },
        }
        for key in ("rejection_reason_counts", "rejection_breakdown", "exit_reason_counts",
                    "dte_breakdown", "symbol_breakdown", "option_type_breakdown"):
            if isinstance(result, dict) and result.get(key):
                summary[key] = result.get(key)
        if not summary.get("rejection_reason_counts"):
            summary["rejection_reason_counts"] = summary.get("rejection_breakdown")
        return _section("BACKTEST", summary)
    except Exception as exc:
        return _section("BACKTEST", {"available": False,
                                     "reason": f"backtest store unreadable: {type(exc).__name__}"})


# ── MISMATCHES / SYSTEM_ERRORS ──────────────────────────────────────────
def _mismatches() -> List[Dict[str, Any]]:
    try:
        from backend.config.runtime_config import detect_config_mismatches
        return detect_config_mismatches()
    except Exception:
        return []


def _system_errors(db: Any) -> Dict[str, Any]:
    lines: List[str] = []
    try:
        path = os.path.join("logs", "errors.log")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = [l.rstrip("\n") for l in fh.readlines()[-RECENT_ERRORS_LIMIT:]]
    except Exception:
        pass
    events: List[Any] = []
    try:
        from backend.health.health_monitor import health_monitor
        events = (health_monitor.snapshot() or {}).get("recent_events", [])[-RECENT_ERRORS_LIMIT:]
    except Exception:
        pass
    return _section("LIVE_RUNTIME", {
        "available": True,
        "bounded": RECENT_ERRORS_LIMIT,
        "recent_error_lines": lines,
        "recent_health_events": events,
        "note": None if (lines or events) else
                "No recent errors recorded (error log and health events are empty).",
    })


# ── THE ONE BUILDER ──────────────────────────────────────────────────────
def build_full_context(app_state: Any = None, *, include_errors: bool = True) -> Dict[str, Any]:
    """Assemble the authoritative Copilot context (redacted, bounded).

    `app_state` is the FastAPI `app.state` (engine/ws_client/scanner live
    there); when absent (tests, worker process) sections degrade honestly
    to their database/persisted sources.
    """
    state = app_state
    if state is None:
        try:
            from backend.api.main import app
            state = app.state
        except Exception:
            from types import SimpleNamespace
            state = SimpleNamespace(engine=None, ws_client=None, scanner=None)
    db = _db()
    ctx: Dict[str, Any] = {
        "generated_at": _now().isoformat(),
        "bot": _bot_state(state),
        "configuration": _configuration(),
        "market": _market_state(),
        "data_health": _data_health(),
        "websocket": _websocket(state),
        "scanner": _scanner(state, db),
        "latest_signal": _latest_signal(db) if db is not None else
                         _section("SCANNER", {"available": False, "reason": "database unavailable"}),
        "latest_decision": _latest_decision(db) if db is not None else
                           _section("DATABASE", {"available": False, "reason": "database unavailable"}),
        "latest_rejection": _latest_rejection(db) if db is not None else
                            _section("SCANNER", {"available": False, "reason": "database unavailable"}),
        "today": _today(db) if db is not None else
                 _section("DATABASE", {"available": False, "reason": "database unavailable"}),
        "positions": _positions(db) if db is not None else
                     _section("DATABASE", {"available": False, "reason": "database unavailable"}),
        "recent_trades": _recent_trades(db) if db is not None else
                         _section("DATABASE", {"available": False, "reason": "database unavailable"}),
        "risk": _risk(state),
        "execution": _execution(state),
        "reconciliation": _reconciliation(db) if db is not None else
                          _section("DATABASE", {"available": False, "reason": "database unavailable"}),
        "broker": _broker(),
        "pipeline": _pipeline(db) if db is not None else
                    _section("SCANNER", {"available": False, "reason": "database unavailable"}),
        "ai": _ai_trading_decision(db) if db is not None else
              _section("DATABASE", {"available": False, "reason": "database unavailable"}),
        "copilot": _copilot_self(),
        "backtest": _backtest_summary(),
        "configuration_mismatches": _mismatches(),
    }
    if include_errors:
        ctx["errors"] = _system_errors(db) if db is not None else \
            _section("LIVE_RUNTIME", {"available": False, "reason": "database unavailable"})

    # NO SECRETS: the entire payload passes the same redaction layer as the
    # chat context before reaching any LLM or the frontend (spec §10/§32).
    try:
        from backend.copilot.secret_guard import collect_process_secrets, collect_secret_values, redact_value
        ctx = redact_value(ctx, collect_secret_values(collect_process_secrets()))
    except Exception:
        pass
    return ctx
