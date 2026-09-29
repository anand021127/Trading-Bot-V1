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

MAX_RECORD_CHARS = 6000
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
        return "NO_SIGNAL"
    return "SIGNAL" if r.startswith(("rejected:", "AI_NO_TRADE", "POSITION_ALREADY_OPEN",
                                    "signal_payload_incomplete")) else "NO_SIGNAL"


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
        risky = body.startswith(("kill_switch", "MAX_", "INSUFFICIENT", "DAILY", "RISK"))
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
    txt = json.dumps(slim, default=str)
    if len(txt) <= MAX_RECORD_CHARS:
        return txt
    core_keys = ("seq", "scanned", "traded", "reason", "signal", "recorded_at",
                 "recorded_at_ist", "strategy", "underlying", "category",
                 "data_status", "error", "duration_ms", "next_scan_at",
                 "risk_decision", "execution_decision")
    core = {k: slim.get(k) for k in core_keys}
    core["details"] = {"recorded_at": slim.get("recorded_at"), "truncated": True,
                       "rejection": (slim.get("rejection") or [])[:3]}
    core["error"] = redact(core.get("error") or "", 200) or None
    return json.dumps(core, default=str)


def persist_scan_record(db: Any, record: Dict[str, Any]) -> None:
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
        hist.append({
            "seq": record.get("seq"), "at": record.get("recorded_at"),
            "reason": str(record.get("reason") or "")[:120],
            "category": record.get("category"), "signal": record.get("signal"),
            "traded": bool(record.get("traded")),
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
    if cat == "NO_SIGNAL":
        why = "; ".join(rej[:3]) if rej else "pullback/reversal criteria were not met"
        return (f"Scanner ran at {when} IST. V8-D evaluated successfully. "
                f"No signal because {why}.")
    if reason.startswith("AI_NO_TRADE"):
        return f"Scanner ran at {when} IST. V8-D produced a BUY but it was blocked ({reason})."
    return (f"Scanner ran at {when} IST. V8-D produced a BUY that was not executed "
            f"({reason or 'no reason recorded'}).")


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
    scan_stale_after = max(45.0, 6.0 * interval)
    market_scan_setting = _get(MARKET_SCAN_KEY) or None

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
    "ALL_STATES", "build_scan_record", "classify_reason", "compute_runtime_state",
    "describe_exception", "persist_scan_record", "read_scan_history", "read_scan_record",
    "redact", "scan_interval_seconds", "summarize_record", "to_ist_str",
]
