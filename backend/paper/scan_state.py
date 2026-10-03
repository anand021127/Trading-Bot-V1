"""Persisted scan records + honest runtime status for the PAPER worker.

WHY THIS MODULE EXISTS
----------------------
The dashboard used to derive "Bot RUNNING / Scanner RUNNING / Market data
STREAMING" from flags and from *API-process* components (BotState flag, the
API's own LiveScanner heartbeat, the API's broker WebSocket object) — none of
which prove that the PAPER WORKER (the only process that runs the V8-D market
scan) is alive or that a scan iteration ever executed. The Copilot then said
"No scan has been recorded" while the header said RUNNING.

This module is the ONE authority for:

  1. ``persist_scan_record`` — every scan iteration (including market-closed,
     data errors, scanner-disabled and exceptions) is persisted as ONE valid,
     size-bounded JSON record with a monotonically increasing ``seq``.
  2. ``compute_runtime_state`` — a typed operational state derived from the
     worker heartbeat + the persisted scan record (never from a flag alone).

No secrets are ever written: error strings are redacted (``redact``).
It never places orders and never touches strategy parameters.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# ── persisted keys (all in the shared SQLite settings table) ────────────────
SCAN_DETAIL_KEY = "paper_worker_last_scan_detail"
SCAN_REASON_KEY = "paper_worker_last_scan"
SCAN_TS_KEY = "paper_worker_last_scan_ts"
SCAN_SEQ_KEY = "paper_worker_scan_seq"
SCAN_INFLIGHT_KEY = "paper_worker_scan_in_progress"
SCAN_HISTORY_KEY = "paper_worker_scan_history"
MARKET_SCAN_KEY = "paper_worker_market_scan"
HB_KEY = "paper_worker_heartbeat"
PID_KEY = "paper_worker_pid"
STATUS_KEY = "paper_worker_status"
ERR_KEY = "paper_worker_last_error"
LOOP_KEY = "paper_worker_loop_count"
SYMBOLS_KEY = "paper_worker_symbol_scans"        # {SYMBOL: compact record} — latest scan per underlying
SYMBOL_LIST_KEY = "paper_worker_scan_symbols"    # JSON list of the underlyings the worker scans
LAST_ACTIVITY_KEY = "paper_worker_last_scan_activity"  # ISO time of the NEWEST scan of ANY symbol

MAX_RECORD_CHARS = 9000
HISTORY_LEN = 20
HEARTBEAT_FRESH_SECONDS = 20.0
START_GRACE_SECONDS = 45.0

# ── runtime states (UI contract) ────────────────────────────────────────────
STOPPED = "STOPPED"
STARTING = "STARTING"
WORKER_NOT_RESPONDING = "STARTED_WORKER_NOT_RESPONDING"
RUNNING_SCANNING = "RUNNING_SCANNING"
RUNNING_WAITING_FOR_MARKET = "RUNNING_WAITING_FOR_MARKET"
RUNNING_NO_SIGNAL = "RUNNING_NO_SIGNAL"
RUNNING_DATA_ERROR = "RUNNING_DATA_ERROR"
RUNNING_SCANNER_ERROR = "RUNNING_SCANNER_ERROR"

ALL_STATES = (
    STOPPED, STARTING, WORKER_NOT_RESPONDING, RUNNING_SCANNING,
    RUNNING_WAITING_FOR_MARKET, RUNNING_NO_SIGNAL, RUNNING_DATA_ERROR,
    RUNNING_SCANNER_ERROR,
)

_STATE_LABEL = {
    STOPPED: "Stopped",
    STARTING: "Starting",
    WORKER_NOT_RESPONDING: "Started — worker not responding",
    RUNNING_SCANNING: "Running — scanning",
    RUNNING_WAITING_FOR_MARKET: "Running — waiting for market",
    RUNNING_NO_SIGNAL: "Running — no signal",
    RUNNING_DATA_ERROR: "Running — data error",
    RUNNING_SCANNER_ERROR: "Running — scanner error",
}
_STATE_SEVERITY = {
    STOPPED: "info", STARTING: "info", WORKER_NOT_RESPONDING: "error",
    RUNNING_SCANNING: "ok", RUNNING_WAITING_FOR_MARKET: "info",
    RUNNING_NO_SIGNAL: "ok", RUNNING_DATA_ERROR: "error",
    RUNNING_SCANNER_ERROR: "error",
}

# ── secret redaction ────────────────────────────────────────────────────────
_REDACTIONS = (
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-~+/=]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)(access[_-]?token|refresh[_-]?token|token|api[_-]?key|client[_-]?secret|secret|password|authorization)([\"'\s:=]+)[^\s\"',;&]{4,}"),
     r"\1\2[REDACTED]"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"), "[REDACTED_JWT]"),
)


def redact(text: Any, limit: int = 300) -> str:
    """Strip token-like material from an error string and bound its length."""
    s = str(text if text is not None else "")
    for rx, repl in _REDACTIONS:
        s = rx.sub(repl, s)
    return s[:limit]


def describe_exception(exc: BaseException) -> str:
    return redact(f"{type(exc).__name__}: {exc}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_ist_str(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _ist_clock(dt: Optional[datetime]) -> str:
    return dt.astimezone(IST).strftime("%H:%M:%S") if dt else "?"


# ── reason classification ───────────────────────────────────────────────────
_DATA_PREFIXES = (
    "candle_fetch_error", "expiry_fetch_error", "chain_fetch_error", "no_candles",
    "insufficient_candles", "stale_candles", "invalid_ohlc", "missing_candle_timestamp",
    "candle_timestamp_in_future", "no_upcoming_expiry", "empty_option_chain",
    "no_valid_spot", "scanner_disabled", "scanner_init_failed",
)
_ERROR_PREFIXES = (
    "strategy_error", "scan_error", "submit_error", "position_check_error",
    "entry_window_error", "scan_timeout",
)


def classify_reason(reason: str, signal: Any = None, traded: bool = False) -> str:
    """Map a persisted scan reason to one of:
    MARKET_CLOSED · DATA_ERROR · SCANNER_ERROR · NO_SIGNAL · SIGNAL."""
    r = str(reason or "")
    if traded or signal == "BUY":
        return "SIGNAL"
    if r == "market_closed" or r.startswith("entry_window_closed"):
        return "MARKET_CLOSED"
    if r.startswith(_ERROR_PREFIXES):
        return "SCANNER_ERROR"
    if r.startswith(_DATA_PREFIXES):
        return "DATA_ERROR"
    if r.startswith("no_trade:"):
        # V8-D's own verdict is the suffix: NO_SIGNAL = the technical setup did not
        # exist; anything else (e.g. REJECTED) = a setup existed but V8-D refused it.
        verdict = r.split(":", 1)[1].strip().upper()
        # NO_SIGNAL (or no verdict at all) = the setup did not exist. Only an
        # explicit V8-D REJECTED verdict means "a setup existed and was refused".
        return "SIGNAL" if verdict.startswith("REJECT") else "NO_SIGNAL"
    return "SIGNAL" if r.startswith(("rejected:", "AI_NO_TRADE", "AI_STRATEGY_MISMATCH", "POSITION_ALREADY_OPEN",
                                    "signal_payload_incomplete", "position_check_error")) else "NO_SIGNAL"


OUTCOMES = ("NO_SIGNAL", "SIGNAL_REJECTED", "RISK_REJECTED", "AI_REJECTED", "EXECUTION_REJECTED",
            "FILLED", "MARKET_CLOSED", "DATA_ERROR", "SCANNER_ERROR", "NO_TRADE")
_RISK_BODY_PREFIXES = ("kill_switch", "MAX_", "INSUFFICIENT", "DAILY", "RISK")


def derive_outcome(reason: str, signal: Any = None, traded: bool = False) -> str:
    """THE one authority for "what stopped this scan" (pipeline, Copilot and API
    all use it):

      NO_SIGNAL           V8-D evaluated; the technical setup did not exist.
      SIGNAL_REJECTED     V8-D found a setup but refused it (contract / sizing / payload).
      AI_REJECTED         the AI Trading Decision gate said REJECT/WAIT or failed safe.
      RISK_REJECTED       kill switch / position / daily-loss / equity / max-trades guard.
      EXECUTION_REJECTED  the pipeline/broker refused or errored.
      FILLED              paper order filled.
      MARKET_CLOSED · DATA_ERROR · SCANNER_ERROR   the strategy was never evaluated.
    NO_SIGNAL is never called a rejected signal."""
    r = str(reason or "")
    if traded:
        return "FILLED"
    if r.startswith("submit_error"):
        return "EXECUTION_REJECTED"
    cat = classify_reason(r, signal, traded)
    if cat in ("MARKET_CLOSED", "DATA_ERROR", "SCANNER_ERROR"):
        return cat
    if cat == "NO_SIGNAL":
        return "NO_SIGNAL"
    if r.startswith(("AI_NO_TRADE", "AI_STRATEGY_MISMATCH")):
        return "AI_REJECTED"
    if r.startswith(("POSITION_ALREADY_OPEN", "position_check_error")):
        return "RISK_REJECTED"
    if r.startswith("rejected:"):
        return "RISK_REJECTED" if r.split(":", 1)[1].startswith(_RISK_BODY_PREFIXES) else "EXECUTION_REJECTED"
    if r.startswith(("no_trade:", "signal_payload_incomplete")):
        return "SIGNAL_REJECTED"
    return "NO_TRADE"


def _decisions(reason: str, signal: Any, traded: bool) -> Dict[str, str]:
    """risk / execution decision strings for the record (diagnostics only)."""
    r = str(reason or "")
    if traded:
        return {"risk_decision": "PASSED", "execution_decision": "FILLED_PAPER"}
    if signal != "BUY":
        return {"risk_decision": "NOT_EVALUATED", "execution_decision": "NOT_ATTEMPTED"}
    if r.startswith("AI_NO_TRADE") or r.startswith("AI_"):
        return {"risk_decision": "NOT_EVALUATED", "execution_decision": "NOT_ATTEMPTED"}
    if r.startswith("POSITION_ALREADY_OPEN"):
        return {"risk_decision": f"REJECTED:{r[:80]}", "execution_decision": "NOT_ATTEMPTED"}
    if r.startswith("rejected:"):
        body = r.split(":", 1)[1]
        risky = body.startswith(_RISK_BODY_PREFIXES)
        return {"risk_decision": f"REJECTED:{body[:80]}" if risky else "PASSED",
                "execution_decision": f"REJECTED:{body[:80]}"}
    if r.startswith("submit_error"):
        return {"risk_decision": "UNKNOWN", "execution_decision": f"ERROR:{r[:80]}"}
    return {"risk_decision": "NOT_EVALUATED", "execution_decision": "NOT_ATTEMPTED"}


def data_status_from_reason(reason: str) -> str:
    r = str(reason or "")
    if r == "market_closed" or r.startswith("entry_window_closed"):
        return "NOT_CHECKED_MARKET_CLOSED"
    if r.startswith(("scanner_disabled", "scanner_init_failed")):
        return "NO_MARKET_DATA_SOURCE"
    if r.startswith("stale_candles") or r.startswith("insufficient_candles") \
            or r == "no_candles" or r.startswith("candle_timestamp"):
        return "STALE_OR_INSUFFICIENT"
    if r.startswith(_DATA_PREFIXES):
        return "ERROR"
    if r.startswith(_ERROR_PREFIXES):
        return "UNKNOWN"
    return "OK"


# ── record building / persistence ───────────────────────────────────────────
def build_scan_record(
    *,
    seq: int,
    scanned: bool,
    traded: bool,
    reason: str,
    signal: Any = None,
    details: Optional[Dict[str, Any]] = None,
    strategy: Optional[str] = None,
    underlying: Optional[str] = None,
    duration_ms: Optional[float] = None,
    next_scan_at: Optional[datetime] = None,
    error: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Assemble ONE scan record. Backward compatible: the historical keys
    (scanned/traded/reason/signal/details) are unchanged; new diagnostics are
    additive and mirrored at top level for convenient reading."""
    now = now or _utc_now()
    det: Dict[str, Any] = dict(details or {})
    # v8d / chain are stored ONCE at the top level of the record (not duplicated in
    # `details`, which doubled the size and forced lossy compaction).
    v8d_full, chain_full = det.pop("v8d", None), det.pop("chain", None)
    det["recorded_at"] = now.isoformat()
    dec = _decisions(reason, signal, traded)
    rejection = list(det.get("rejection") or [])
    record: Dict[str, Any] = {
        "seq": int(seq),
        "scanned": bool(scanned),
        "traded": bool(traded),
        "reason": str(reason or ""),
        "signal": signal,
        "details": det,
        "recorded_at": now.isoformat(),
        "recorded_at_ist": to_ist_str(now),
        "strategy": strategy or det.get("strategy"),
        "underlying": underlying or det.get("underlying"),
        "category": classify_reason(reason, signal, traded),
        "data_status": det.get("data_status") or data_status_from_reason(reason),
        "session_status": det.get("session_status"),
        "candle_count": det.get("candle_count", det.get("bars")),
        "last_candle_ts": det.get("last_candle_ts"),
        "candle_age_seconds": det.get("candle_age_seconds"),
        "expiry": det.get("expiry"),
        "option_chain_count": det.get("option_chain_count"),
        "spot": det.get("spot"),
        "selected_contract": det.get("selected_contract"),
        "decision": det.get("decision"),
        "ai": det.get("ai"),
        "outcome": derive_outcome(reason, signal, traded),
        "v8d": v8d_full,
        "chain": chain_full,
        "candle_complete": det.get("last_candle_complete"),
        "rejection": rejection,
        "risk_decision": dec["risk_decision"],
        "execution_decision": dec["execution_decision"],
        "error": redact(error) if error else None,
        "duration_ms": round(float(duration_ms), 1) if duration_ms is not None else None,
        "next_scan_at": next_scan_at.isoformat() if next_scan_at else None,
    }
    return record


def _bounded_json(record: Dict[str, Any]) -> str:
    """Serialise to VALID JSON under MAX_RECORD_CHARS.

    The previous implementation did ``json.dumps(...)[:2000]`` which slices
    the string mid-token and yields CORRUPT JSON for any richer payload —
    the reader then reported 'persisted scan detail is corrupt'. We shrink
    fields instead of slicing text."""
    txt = json.dumps(record, default=str)
    if len(txt) <= MAX_RECORD_CHARS:
        return txt
    slim = dict(record)
    det = dict(slim.get("details") or {})
    for k in ("ai_reason_codes", "rejection", "selected_contract"):
        v = det.get(k)
        if isinstance(v, list):
            det[k] = [str(x)[:120] for x in v[:5]]
        elif isinstance(v, dict):
            det[k] = {kk: v[kk] for kk in list(v)[:8]}
    slim["details"] = det
    slim["rejection"] = [str(x)[:120] for x in (slim.get("rejection") or [])[:5]]
    slim["selected_contract"] = None
    slim["v8d"] = compact_v8d(slim.get("v8d"))
    slim["chain"] = None
    txt = json.dumps(slim, default=str)
    if len(txt) <= MAX_RECORD_CHARS:
        return txt
    core_keys = ("seq", "scanned", "traded", "reason", "signal", "recorded_at",
                 "recorded_at_ist", "strategy", "underlying", "category",
                 "data_status", "error", "duration_ms", "next_scan_at",
                 "risk_decision", "execution_decision", "outcome")
    core = {k: slim.get(k) for k in core_keys}
    core["details"] = {"recorded_at": slim.get("recorded_at"), "truncated": True,
                       "rejection": (slim.get("rejection") or [])[:3]}
    core["error"] = redact(core.get("error") or "", 200) or None
    return json.dumps(core, default=str)


def compact_v8d(v: Any) -> Any:
    """Keep the numbers + pass/fail flags, drop the long per-condition prose."""
    if not isinstance(v, dict):
        return v
    out = {k: v.get(k) for k in ("evaluated", "decision", "decision_label", "candle_count", "ema20", "ema50",
                                  "ema_separation_pct", "rsi", "atr14_underlying", "price", "pullback_band", "closest_side",
                                  "binding_condition", "failed", "reason", "strategy_decision", "consistent")
           if k in v}
    for side in ("ce", "pe"):
        if isinstance(v.get(side), dict):
            out[side] = {k: (v[side][k].get("pass") if isinstance(v[side].get(k), dict) else v[side].get(k))
                         for k in ("trend", "pullback", "rsi", "reversal", "all_pass", "failed") if k in v[side]}
    return out


_OUTCOME_RANK = {"FILLED": 0, "AI_REJECTED": 1, "RISK_REJECTED": 1, "EXECUTION_REJECTED": 1,
                 "SIGNAL_REJECTED": 2, "NO_SIGNAL": 3, "MARKET_CLOSED": 4, "DATA_ERROR": 5,
                 "SCANNER_ERROR": 6, "NO_TRADE": 7}


def symbol_summary(record: Dict[str, Any]) -> Dict[str, Any]:
    """Compact per-underlying row for the coverage table (size-bounded)."""
    v = record.get("v8d") or {}
    px = v.get("price") or {}
    return {
        "symbol": record.get("underlying"), "seq": record.get("seq"),
        "recorded_at": record.get("recorded_at"), "recorded_at_ist": record.get("recorded_at_ist"),
        "outcome": record.get("outcome"), "reason": str(record.get("reason") or "")[:160],
        "category": record.get("category"), "signal": record.get("signal"), "traded": bool(record.get("traded")),
        "candle_count": record.get("candle_count"), "last_candle_ts": record.get("last_candle_ts"),
        "candle_complete": record.get("candle_complete"),
        "option_chain_count": record.get("option_chain_count"), "expiry": record.get("expiry"),
        "v8d_label": v.get("decision_label"), "binding": v.get("binding_condition"),
        "failed": v.get("failed"), "closest_side": v.get("closest_side"),
        "ema20": v.get("ema20"), "ema50": v.get("ema50"), "sep_pct": v.get("ema_separation_pct"),
        "rsi": v.get("rsi"), "close": px.get("close"),
        "ai_status": (record.get("ai") or {}).get("status"), "error": record.get("error"),
    }


def persist_symbol_record(db: Any, record: Dict[str, Any]) -> None:
    """Merge this symbol's latest compact record into the per-underlying map. Never raises."""
    try:
        sym = str(record.get("underlying") or "").upper()
        if not sym:
            return
        try:
            cur = json.loads(db.get_setting(SYMBOLS_KEY, "") or "{}")
            if not isinstance(cur, dict):
                cur = {}
        except Exception:
            cur = {}
        cur[sym] = symbol_summary(record)
        db.save_setting(SYMBOLS_KEY, json.dumps(cur, default=str))
        db.save_setting(LAST_ACTIVITY_KEY, str(record.get("recorded_at") or ""))
    except Exception:
        return


def read_symbol_records(db: Any) -> List[Dict[str, Any]]:
    try:
        cur = json.loads(db.get_setting(SYMBOLS_KEY, "") or "{}")
        rows = [v for v in cur.values() if isinstance(v, dict)] if isinstance(cur, dict) else []
    except Exception:
        return []
    return sorted(rows, key=lambda r: str(r.get("symbol") or ""))


def pick_primary(records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The most informative FULL record among the latest per-symbol scans:
    a fill beats an AI/risk/execution rejection beats a V8-D-rejected setup beats
    NO_SIGNAL beats 'market closed' beats data/scanner errors; ties → newest."""
    best = None
    for r in records:
        if not r:
            continue
        key = (_OUTCOME_RANK.get(str(r.get("outcome") or "NO_TRADE"), 7), -_ts(r.get("recorded_at")))
        if best is None or key < best[0]:
            best = (key, r)
    return best[1] if best else None


def _ts(raw: Any) -> float:
    d = _parse_iso(raw)
    return d.timestamp() if d else 0.0


PAPER_DEFAULT_UNDERLYINGS = ("NIFTY50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX")


def parse_underlyings(env: Optional[Dict[str, str]] = None) -> List[str]:
    """Which underlyings the paper scanner evaluates.

      PAPER_UNDERLYINGS = NIFTY50,SENSEX | ALL     (preferred)
      PAPER_UNDERLYING  = NIFTY50                  (legacy single symbol)
      default           = all six supported underlyings — the same universe the
                          backtest defaults to (VALID_OPTION_INDICES). Scanning
                          fewer symbols than the strategy/backtest universe is
                          opt-in, never the silent default.
    Unknown names are ignored (never invented)."""
    e = env if env is not None else os.environ
    raw = (e.get("PAPER_UNDERLYINGS") or "").strip()
    if not raw:
        raw = (e.get("PAPER_UNDERLYING") or "").strip()
    if not raw or raw.upper() == "ALL":
        return list(PAPER_DEFAULT_UNDERLYINGS)
    out: List[str] = []
    for part in raw.replace(";", ",").split(","):
        name = part.strip().upper()
        if name in PAPER_DEFAULT_UNDERLYINGS and name not in out:
            out.append(name)
    return out or list(PAPER_DEFAULT_UNDERLYINGS)


def persist_scan_record(db: Any, record: Dict[str, Any], history_record: Optional[Dict[str, Any]] = None) -> None:
    """Persist the record + a compact history entry. Never raises."""
    try:
        db.save_setting(SCAN_DETAIL_KEY, _bounded_json(record))
        db.save_setting(SCAN_REASON_KEY, str(record.get("reason") or ""))
        db.save_setting(SCAN_TS_KEY, str(record.get("recorded_at") or ""))
        db.save_setting(SCAN_SEQ_KEY, str(int(record.get("seq") or 0)))
    except Exception:
        return
    try:
        raw = db.get_setting(SCAN_HISTORY_KEY, "") or "[]"
        try:
            hist: List[Dict[str, Any]] = json.loads(raw)
            if not isinstance(hist, list):
                hist = []
        except Exception:
            hist = []
        h = history_record or record
        hist.append({
            "seq": h.get("seq"), "at": h.get("recorded_at"), "symbol": h.get("underlying"),
            "reason": str(h.get("reason") or "")[:120],
            "category": h.get("category"), "outcome": h.get("outcome"), "signal": h.get("signal"),
            "traded": bool(h.get("traded")),
        })
        db.save_setting(SCAN_HISTORY_KEY, json.dumps(hist[-HISTORY_LEN:]))
    except Exception:
        pass


def read_scan_record(db: Any) -> Optional[Dict[str, Any]]:
    try:
        raw = db.get_setting(SCAN_DETAIL_KEY, "") or ""
        if not raw:
            return None
        rec = json.loads(raw)
        return rec if isinstance(rec, dict) else None
    except Exception:
        return None


def read_scan_history(db: Any) -> List[Dict[str, Any]]:
    try:
        v = json.loads(db.get_setting(SCAN_HISTORY_KEY, "") or "[]")
        return v if isinstance(v, list) else []
    except Exception:
        return []


def symbol_scan_interval_seconds(n_symbols: int = 1) -> float:
    """Minimum seconds between scans of ONE underlying. With several underlyings
    each is re-scanned at most every 20 s (V8-D only changes when a 5-minute bar
    closes; the cycle must stay well inside the Upstox request budget)."""
    base = scan_interval_seconds()
    if n_symbols <= 1:
        return base
    try:
        return max(base, float(os.environ.get("PAPER_SYMBOL_SCAN_INTERVAL_SEC", "20")))
    except (TypeError, ValueError):
        return max(base, 20.0)


def scan_interval_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get("PAPER_SCAN_INTERVAL_SEC", "5")))
    except (TypeError, ValueError):
        return 5.0


# ── human summary ───────────────────────────────────────────────────────────
def summarize_record(rec: Dict[str, Any]) -> str:
    """One honest sentence, e.g. 'Scanner ran at 10:23:15 IST. V8-D evaluated
    successfully. No signal because pullback/reversal criteria were not met.'"""
    when = _ist_clock(_parse_iso(rec.get("recorded_at")))
    reason = str(rec.get("reason") or "")
    cat = rec.get("category") or classify_reason(reason, rec.get("signal"), rec.get("traded"))
    rej = [str(x) for x in (rec.get("rejection") or (rec.get("details") or {}).get("rejection") or [])]
    if rec.get("traded"):
        return f"Scanner ran at {when} IST. V8-D produced a BUY and the paper trade was filled."
    if cat == "MARKET_CLOSED":
        return (f"Scanner ran at {when} IST but is not trading because the market/entry window "
                f"is closed ({reason}). The scan loop itself is executing.")
    if cat == "DATA_ERROR":
        return (f"Scanner ran at {when} IST but market data is not usable ({reason}). "
                "V8-D was not evaluated.")
    if cat == "SCANNER_ERROR":
        err = rec.get("error") or reason
        return f"Scanner iteration at {when} IST failed ({err}). V8-D was not evaluated."
    if cat == "SIGNAL" and str(rec.get("outcome") or "") == "SIGNAL_REJECTED":
        return (f"Scanner ran at {when} IST. V8-D found a setup but REJECTED the signal "
                f"({'; '.join(rej[:3]) if rej else reason}). No trade.")
    if cat == "NO_SIGNAL":
        v8 = rec.get("v8d") or {}
        why = (v8.get("reason") if v8.get("evaluated") and v8.get("reason")
               else "; ".join(rej[:3]) if rej else "pullback/reversal criteria were not met")
        return (f"Scanner ran at {when} IST. V8-D evaluated successfully. "
                f"No signal because {why}.")
    if reason.startswith("AI_NO_TRADE"):
        return f"Scanner ran at {when} IST. V8-D produced a BUY but it was blocked ({reason})."
    return (f"Scanner ran at {when} IST. V8-D produced a BUY that was not executed "
            f"({reason or 'no reason recorded'}).")


# ── decision pipeline view (what the operator sees) ─────────────────────────
_SCANNER_LABEL = {
    STOPPED: "STOPPED", STARTING: "STARTING", WORKER_NOT_RESPONDING: "NOT RESPONDING",
    RUNNING_SCANNING: "RUNNING", RUNNING_WAITING_FOR_MARKET: "RUNNING",
    RUNNING_NO_SIGNAL: "RUNNING", RUNNING_DATA_ERROR: "RUNNING — DATA ERROR",
    RUNNING_SCANNER_ERROR: "RUNNING — SCANNER ERROR",
}


def build_pipeline(rec: Optional[Dict[str, Any]], state: str, *, ai_fallback: Optional[Dict[str, Any]] = None,
                   symbols: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """ONE honest, UI-ready view of the decision pipeline for the latest scan:

        Scanner · Market · Strategy · Latest Signal · AI Decision · AI Reason ·
        Risk Check · Execution

    Every value is derived from the persisted scan record — never from a flag —
    and every 'did not happen' case says so explicitly (NOT EVALUATED, DISABLED,
    MARKET CLOSED …) instead of implying an evaluation that never ran."""
    scanner = _SCANNER_LABEL.get(state, state)
    base: Dict[str, Any] = {
        "scanner": scanner,
        "market": "UNKNOWN",
        "strategy": os.environ.get("TRADING_STRATEGY", "V8_D_PULLBACK_ATM"),
        "latest_signal": "NO SCAN YET",
        "signal_detail": None,
        "ai_decision": "NOT EVALUATED",
        "ai_reason": None,
        "ai_confidence": None,
        "ai_latency_ms": None,
        "ai_enabled": bool((ai_fallback or {}).get("enabled")) if ai_fallback else None,
        "risk_check": "NOT EVALUATED",
        "risk_detail": None,
        "execution": "NOT ATTEMPTED",
        "execution_detail": None,
        "outcome": None,            # NO_SIGNAL | SIGNAL_REJECTED | AI_REJECTED | RISK_REJECTED | EXECUTION_REJECTED | FILLED | MARKET_CLOSED | DATA_ERROR | SCANNER_ERROR
        "final": "NO TRADE",        # the only two final states: NO TRADE | FILLED (PAPER)
        "v8d": None,                # per-condition V8-D diagnostics for the primary symbol
        "primary_symbol": None,
        "symbols": list(symbols or []),   # latest scan of EVERY scanned underlying
        "scan_seq": None,
        "scan_time_ist": None,
        "summary": "No scan has been recorded yet.",
    }
    if not rec:
        if ai_fallback:
            base["ai_decision"] = "DISABLED" if not ai_fallback.get("enabled") else "NOT EVALUATED"
            base["ai_reason"] = ai_fallback.get("reason")
        return base

    reason = str(rec.get("reason") or "")
    cat = rec.get("category") or classify_reason(reason, rec.get("signal"), rec.get("traded"))
    ai = rec.get("ai") or {}
    det = rec.get("details") or {}
    sess = str(rec.get("session_status") or det.get("session_status") or "").upper()
    traded = bool(rec.get("traded"))
    is_buy = traded or rec.get("signal") == "BUY"
    contract = rec.get("selected_contract") or det.get("selected_contract") or {}
    otype = str((contract or {}).get("option_type") or "").upper()

    outcome = rec.get("outcome") or derive_outcome(reason, rec.get("signal"), rec.get("traded"))
    base["outcome"] = outcome
    base["v8d"] = rec.get("v8d") or det.get("v8d")
    base["primary_symbol"] = rec.get("underlying")
    base["scan_seq"] = rec.get("seq")
    base["scan_time_ist"] = rec.get("recorded_at_ist")
    base["strategy"] = rec.get("strategy") or base["strategy"]

    # Market
    if cat == "MARKET_CLOSED" or (sess and sess != "OPEN"):
        base["market"] = "MARKET CLOSED"
    elif reason.startswith("entry_window_closed"):
        base["market"] = "LIVE — ENTRY WINDOW CLOSED"
    else:
        base["market"] = "LIVE"

    # Latest signal
    if is_buy:
        base["latest_signal"] = f"BUY {otype}".strip()
        base["signal_detail"] = "V8-D produced a BUY signal."
    elif outcome == "SIGNAL_REJECTED":
        base["latest_signal"] = "SIGNAL REJECTED (BY V8-D)"
        rej = [str(x) for x in (rec.get("rejection") or det.get("rejection") or [])]
        base["signal_detail"] = "; ".join(rej[:3]) if rej else reason
    elif cat == "NO_SIGNAL":
        base["latest_signal"] = "NO SIGNAL"
        v8 = base["v8d"] or {}
        rej = [str(x) for x in (rec.get("rejection") or det.get("rejection") or [])]
        base["signal_detail"] = (v8.get("reason") if v8.get("evaluated") and v8.get("reason") else
                                 "; ".join(rej[:3]) if rej else "V8-D evaluated: pullback/reversal criteria not met.")
    elif cat == "MARKET_CLOSED":
        base["latest_signal"] = "NOT EVALUATED — MARKET CLOSED"
        base["signal_detail"] = reason
    elif cat == "DATA_ERROR":
        base["latest_signal"] = "NOT EVALUATED — DATA PROBLEM"
        base["signal_detail"] = rec.get("error") or reason
    else:
        base["latest_signal"] = "NOT EVALUATED — SCANNER ERROR"
        base["signal_detail"] = rec.get("error") or reason

    # AI decision
    ai_status = str(ai.get("status") or "")
    if ai_status == "APPROVED":
        base["ai_decision"] = "APPROVED"
    elif ai_status == "REJECTED":
        base["ai_decision"] = "REJECTED"
    elif ai_status == "WAIT":
        base["ai_decision"] = "WAIT (NO TRADE)"
    elif ai_status == "UNAVAILABLE":
        base["ai_decision"] = "UNAVAILABLE — FAILED SAFE (NO TRADE)"
    elif ai_status == "DISABLED":
        base["ai_decision"] = "DISABLED"
    else:
        base["ai_decision"] = "NOT EVALUATED"
    base["ai_enabled"] = ai.get("enabled") if "enabled" in ai else base["ai_enabled"]
    base["ai_confidence"] = ai.get("confidence") if ai_status in ("APPROVED", "REJECTED", "WAIT", "UNAVAILABLE") else None
    base["ai_latency_ms"] = ai.get("latency_ms") if ai_status in ("APPROVED", "REJECTED", "WAIT", "UNAVAILABLE") else None
    if ai_status in ("APPROVED", "REJECTED", "WAIT", "UNAVAILABLE"):
        codes = ", ".join(ai.get("reason_codes") or [])
        base["ai_reason"] = " — ".join(x for x in (codes, ai.get("reason")) if x) or None
    elif ai_status == "DISABLED":
        base["ai_reason"] = ai.get("reason")
    else:
        if not is_buy:
            why = ("market closed" if cat == "MARKET_CLOSED" else
                   "no usable market data" if cat == "DATA_ERROR" else
                   "scanner error" if cat == "SCANNER_ERROR" else
                   "V8-D rejected the signal first" if outcome == "SIGNAL_REJECTED" else "no V8-D BUY signal")
            base["ai_reason"] = f"Not consulted: {why}."
        else:
            base["ai_reason"] = ai.get("reason") or "AI was not consulted for this signal."

    # Risk check — only meaningful once the AI gate (if any) let the BUY through
    rdec = str(rec.get("risk_decision") or "")
    blocked_by_ai = ai_status in ("REJECTED", "WAIT", "UNAVAILABLE")
    if not is_buy:
        base["risk_check"] = "NOT EVALUATED"
        base["risk_detail"] = ("Stopped earlier by V8-D." if outcome == "SIGNAL_REJECTED"
                               else "No signal reached the risk gate.")
    elif blocked_by_ai:
        base["risk_check"] = "NOT EVALUATED"
        base["risk_detail"] = "Stopped earlier by the AI gate."
    elif rdec == "PASSED":
        base["risk_check"] = "PASS"
    elif rdec.startswith("REJECTED"):
        base["risk_check"] = "REJECTED"
        base["risk_detail"] = rdec.split(":", 1)[-1]
    else:
        base["risk_check"] = "NOT EVALUATED"
        base["risk_detail"] = reason or None

    # Execution
    edec = str(rec.get("execution_decision") or "")
    if traded:
        base["execution"] = "FILLED (PAPER)"
    elif edec.startswith("REJECTED"):
        base["execution"] = "REJECTED"
        base["execution_detail"] = edec.split(":", 1)[-1]
    elif edec.startswith("ERROR"):
        base["execution"] = "ERROR"
        base["execution_detail"] = edec.split(":", 1)[-1]
    else:
        base["execution"] = "NOT ATTEMPTED"     # no order was ever built — distinct from a rejected order
        base["execution_detail"] = None
    base["final"] = "FILLED (PAPER)" if traded else "NO TRADE"

    # One-sentence explanation
    when = _ist_clock(_parse_iso(rec.get("recorded_at")))
    if cat in ("MARKET_CLOSED", "DATA_ERROR", "SCANNER_ERROR") and not is_buy:
        base["summary"] = summarize_record(rec)
    elif not is_buy:
        base["summary"] = summarize_record(rec) + (
            f" AI: {base['ai_decision'].lower()}." if ai_status == "DISABLED" else "")
    elif traded:
        base["summary"] = (f"Scanner ran at {when} IST. V8-D produced {base['latest_signal']}; "
                           f"AI {base['ai_decision']}; risk PASS; paper order FILLED.")
    elif blocked_by_ai:
        base["summary"] = (f"Scanner ran at {when} IST. V8-D produced {base['latest_signal']} but the AI "
                           f"gate stopped it: {base['ai_decision']}"
                           f"{' (' + base['ai_reason'] + ')' if base['ai_reason'] else ''}. No trade.")
    else:
        base["summary"] = (f"Scanner ran at {when} IST. V8-D produced {base['latest_signal']}; AI "
                           f"{base['ai_decision']}; risk {base['risk_check']}; execution {base['execution']}"
                           f"{' (' + (base['execution_detail'] or base['risk_detail'] or reason) + ')' if (base['execution_detail'] or base['risk_detail'] or reason) else ''}.")
    return base


# ── runtime state ───────────────────────────────────────────────────────────
def _default_pid_alive(pid: int) -> bool:
    from backend.paper.worker_lock import pid_is_alive
    return pid_is_alive(pid)


def compute_runtime_state(
    db: Any,
    *,
    now: Optional[datetime] = None,
    pid_alive: Optional[Callable[[int], bool]] = None,
    interval_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """Derive the REAL operational state from persisted facts.

    Never returns RUNNING_* merely because a flag is set: RUNNING_* requires a
    live worker process, a fresh heartbeat AND a scan record newer than the
    start request. Every non-happy state names exactly what is missing."""
    now = now or _utc_now()
    pid_alive = pid_alive or _default_pid_alive
    interval = interval_seconds or scan_interval_seconds()

    def _get(key: str, default: str = "") -> str:
        try:
            return db.get_setting(key, default) or default
        except Exception:
            return default

    running_flag = _get("bot_state_running", "false") == "true"
    killed = _get("bot_state_kill_switch", "false") == "true"
    start_dt = _parse_iso(_get("bot_state_start_time"))
    # No recorded start time on a "running" flag = legacy/unknown: never grant
    # the STARTING grace period (uptime treated as unbounded).
    uptime = ((now - start_dt).total_seconds() if start_dt
              else float("inf")) if running_flag else 0.0

    pid_raw = _get(PID_KEY)
    try:
        pid = int(pid_raw) if pid_raw else 0
    except ValueError:
        pid = 0
    try:
        alive = bool(pid and pid_alive(pid))
    except Exception:
        alive = False
    hb_dt = _parse_iso(_get(HB_KEY))
    hb_age = (now - hb_dt).total_seconds() if hb_dt else None
    hb_fresh = hb_age is not None and hb_age < HEARTBEAT_FRESH_SECONDS
    worker_status = _get(STATUS_KEY, "unknown")
    last_error = _get(ERR_KEY) or None

    rec = read_scan_record(db)
    rec_dt = _parse_iso((rec or {}).get("recorded_at"))
    scan_age = (now - rec_dt).total_seconds() if rec_dt else None
    # A record from BEFORE this start request belongs to a previous run.
    rec_current = bool(rec and rec_dt and (start_dt is None or rec_dt >= start_dt))
    inflight_dt = _parse_iso(_get(SCAN_INFLIGHT_KEY))
    inflight_age = (now - inflight_dt).total_seconds() if inflight_dt else None
    scan_in_progress = inflight_age is not None and inflight_age < 120.0
    # The persisted record is the most informative one of the latest per-symbol
    # scans; "when did the scanner last run" is the NEWEST scan of ANY symbol.
    all_symbol_rows = read_symbol_records(db)
    symbols_current = [r for r in all_symbol_rows
                       if start_dt is None or (_parse_iso(r.get("recorded_at")) or start_dt) >= start_dt]
    activity_dt = _parse_iso(_get(LAST_ACTIVITY_KEY)) or rec_dt
    if activity_dt is not None and (rec_dt is None or activity_dt > rec_dt):
        scan_age = (now - activity_dt).total_seconds()
    n_symbols = max(1, len(all_symbol_rows))
    scan_stale_after = max(45.0, 6.0 * interval, 3.0 * symbol_scan_interval_seconds(n_symbols) + 15.0 * n_symbols)
    market_scan_setting = _get(MARKET_SCAN_KEY) or None

    # AI layer hint for states where no scan record exists yet (env + operator toggle)
    ai_hint: Dict[str, Any] = {}
    try:
        _ov = _get("ai_decision_enabled_override", "")
        _env_on = os.environ.get("AI_DECISION_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
        _on = True if _ov == "1" else False if _ov == "0" else _env_on
        ai_hint = {"enabled": _on, "reason": (
            "AI Trading Decision engine is enabled — waiting for a V8-D BUY to evaluate." if _on else
            "AI Trading Decision engine is disabled — V8-D + risk controls only.")}
    except Exception:
        ai_hint = {}

    base: Dict[str, Any] = {
        "bot_running": running_flag,
        "kill_switch_active": killed,
        "worker_alive": alive,
        "worker_pid": pid if alive else None,
        "worker_status": worker_status,
        "heartbeat_age_seconds": round(hb_age, 1) if hb_age is not None else None,
        "worker_loop_count": _get(LOOP_KEY) or None,
        "uptime_seconds": (round(uptime, 1) if uptime != float("inf") else None),
        "scan_in_progress": scan_in_progress,
        "scan_seq": (rec or {}).get("seq"),
        "scan_age_seconds": round(scan_age, 1) if scan_age is not None else None,
        "scan_interval_seconds": interval,
        "market_scan_setting": market_scan_setting,
        "last_error": last_error,
        "generated_at": now.isoformat(),
        "last_scan": rec if rec_current else None,
        "last_scan_is_stale_run": bool(rec and not rec_current),
    }

    def out(state: str, summary: str) -> Dict[str, Any]:
        d = dict(base)
        d["pipeline"] = build_pipeline(rec if rec_current else None, state, ai_fallback=ai_hint,
                                       symbols=symbols_current)
        d.update({"state": state, "label": _STATE_LABEL[state],
                  "severity": _STATE_SEVERITY[state], "summary": summary,
                  "scanning": state in (RUNNING_SCANNING, RUNNING_NO_SIGNAL,
                                        RUNNING_WAITING_FOR_MARKET)})
        return d

    if killed:
        return out(STOPPED, "Kill switch is ACTIVE — trading is halted until it is reset.")
    if not running_flag:
        extra = " (worker process is idle, waiting for Start)" if alive and hb_fresh else ""
        return out(STOPPED, f"Bot is stopped{extra}.")

    # Bot flagged running from here on — prove it.
    if not alive or not hb_fresh:
        if uptime < START_GRACE_SECONDS:
            return out(STARTING, "Start requested — waiting for the paper worker process to come up "
                                 "and write its first heartbeat.")
        why = ("no worker process is alive" if not alive
               else f"the worker heartbeat is {round(hb_age or 0)}s old")
        extra = f" Last worker error: {last_error}." if last_error else ""
        return out(WORKER_NOT_RESPONDING,
                   f"Bot is flagged RUNNING but {why}, so NO scans are executing.{extra} "
                   "Press Stop then Start to respawn the paper worker.")

    # Worker alive + heartbeat fresh.
    if market_scan_setting and (market_scan_setting.startswith("disabled") or
                                market_scan_setting.startswith("init_failed")):
        # Scanner object does not exist — the worker records this every tick,
        # but surface it even before the first record lands.
        if not rec_current:
            return out(RUNNING_DATA_ERROR,
                       f"Worker is alive but the market-data scanner is not armed "
                       f"({market_scan_setting}). No Upstox token / market-data source.")

    if not rec_current:
        if scan_in_progress:
            return out(RUNNING_SCANNING, "First scan iteration is in progress.")
        if uptime < START_GRACE_SECONDS + 3 * interval:
            return out(STARTING, "Worker is alive; waiting for the first scan iteration to be recorded.")
        return out(RUNNING_SCANNER_ERROR,
                   f"Worker is alive (heartbeat {round(hb_age or 0)}s) but NO scan has been recorded "
                   f"{round(uptime)}s after Start — the scan loop is not producing records.")

    assert rec is not None
    if scan_age is not None and scan_age > scan_stale_after and not scan_in_progress:
        return out(RUNNING_SCANNER_ERROR,
                   f"Worker heartbeat is fresh but the last scan record is {round(scan_age)}s old "
                   f"(expected every ~{round(interval)}s) — the scan loop appears stalled.")

    cat = rec.get("category") or classify_reason(rec.get("reason"), rec.get("signal"), rec.get("traded"))
    summary = summarize_record(rec)
    if cat == "SCANNER_ERROR":
        return out(RUNNING_SCANNER_ERROR, summary)
    if cat == "DATA_ERROR":
        return out(RUNNING_DATA_ERROR, summary)
    if cat == "MARKET_CLOSED":
        return out(RUNNING_WAITING_FOR_MARKET, summary)
    if cat == "NO_SIGNAL":
        return out(RUNNING_NO_SIGNAL, summary)
    return out(RUNNING_SCANNING, summary)


__all__ = [
    "ALL_STATES", "OUTCOMES", "PAPER_DEFAULT_UNDERLYINGS", "parse_underlyings", "build_pipeline", "build_scan_record", "derive_outcome", "classify_reason", "compute_runtime_state",
    "describe_exception", "persist_scan_record", "read_scan_history", "read_scan_record",
    "redact", "scan_interval_seconds", "summarize_record", "to_ist_str",
]
