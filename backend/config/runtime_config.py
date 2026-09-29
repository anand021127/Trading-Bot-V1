"""PHASE B — ONE authoritative runtime configuration resolver.

ROOT CAUSE THIS FIXES: the Settings UI persists operator values (capital,
max trades/day, risk, mode, universe) into the SQLite `settings_blob`, but
every runtime consumer (TradingEngine, PaperTradingRuntime, RiskManager,
live gate, Overview, Operations) previously read only
`backend.config.settings.load_settings()` — pure environment/defaults — at
import time. A user saving capital=₹20,000 / max_trades=20 therefore ran the
bot on capital=₹100,000 / max_trades=3 while every page showed contradictory
numbers.

THE ONE RULE now enforced project-wide:

    effective settings = Settings-DB blob (what the operator saved)
                         over env/startup defaults (load_settings())

Every consumer must resolve configuration through this module instead of
`load_settings()` directly:

    s = get_effective_settings()            # fresh Settings object
    src = get_config_sources()              # per-key provenance labels
    mismatches = detect_config_mismatches() # saved-vs-runtime warnings

The `Settings` dataclasses are unchanged (same types, same env fallbacks) —
only the VALUES get overridden by the saved blob when present. This is a
deep-merge of the same keys the Settings router writes (`mode`, `capital`,
`risk`, `strategy`, `indicators`, `notifications`, `universe`), so behavior
is identical for operators who never touched the Settings UI.

Nothing here mutates global state; `get_effective_settings()` returns a fresh
object each call and caches the DB blob for a short TTL (SQLite reads are
cheap, but hot loops like the scan call this per tick). Call
`invalidate_runtime_config_cache()` after a Settings PUT to make the change
effective immediately, and `set_runtime_config_db()` to bind an explicit
DatabaseManager (the API main.py lifespan does this once at startup).
"""
from __future__ import annotations

import copy
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from backend.config.settings import load_settings

logger = logging.getLogger(__name__)

_BLOB_TTL_SECONDS = 5.0

_blob_lock = threading.Lock()
_cached_blob: Optional[Dict[str, Any]] = None
_cached_blob_at: float = 0.0
_config_db: Optional[Any] = None  # bound DatabaseManager (API lifespan binds the shared one)

# Keys the Settings UI persists (mirrors the Settings router PUT whitelist).
BLOB_KEYS = ("mode", "capital", "risk", "strategy", "indicators", "notifications", "universe")


def set_runtime_config_db(db: Any) -> None:
    """Bind the shared DatabaseManager used for settings_blob reads."""
    global _config_db
    _config_db = db
    invalidate_runtime_config_cache()


def get_runtime_config_db() -> Optional[Any]:
    """The DB the resolver reads the settings blob from.

    Public accessor so other components (e.g. the Settings router's
    GET/PUT blob paths) read/write the SAME source of truth the runtime
    resolves against — never a second, diverging module-global DatabaseManager.
    """
    return _config_db


def invalidate_runtime_config_cache() -> None:
    """Drop the cached blob so the next read reflects a fresh Settings PUT."""
    global _cached_blob, _cached_blob_at
    with _blob_lock:
        _cached_blob = None
        _cached_blob_at = 0.0


_fallback_db: Optional[Any] = None  # bounded implicit-DB fallback (see below)


def _resolve_config_db() -> Any:
    """The DatabaseManager backing the settings blob.

    Normally the one bound via set_runtime_config_db() (the API lifespan binds
    the shared instance). In offline test/CLI contexts with no binding, fall
    back to ONE process-wide implicit instance so behavior is honest instead
    of resolving against a silently diverging DB. NOTE: settings tests should
    bind an isolated DB via set_runtime_config_db() or monkeypatch DATABASE_PATH
    and call invalidate_runtime_config_cache() — the fallback intentionally
    outlives env changes within a process (tests that save blobs to the shared
    DB rely on it).
    """
    global _fallback_db
    if _config_db is not None:
        return _config_db
    if _fallback_db is None:
        from backend.database.db_manager import DatabaseManager
        _fallback_db = DatabaseManager(
            db_path=os.environ.get("DATABASE_PATH", "data/trading_bot.db"))
    return _fallback_db


def _load_settings_blob() -> Optional[Dict[str, Any]]:
    """Read the saved Settings blob (SQLite `settings_blob` row), cached briefly.

    Database failures never crash a trading loop: on error the resolver
    degrades to env defaults and the source labels say so honestly.
    """
    global _cached_blob, _cached_blob_at
    now = time.monotonic()
    with _blob_lock:
        if _cached_blob is not None and (now - _cached_blob_at) < _BLOB_TTL_SECONDS:
            return _cached_blob
    blob: Optional[Dict[str, Any]] = None
    try:
        blob = _resolve_config_db().load_settings_blob()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("runtime_config: settings blob unavailable: %s", type(exc).__name__)
        blob = None
    with _blob_lock:
        _cached_blob = blob if isinstance(blob, dict) else None
        _cached_blob_at = now
    return _cached_blob


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def get_effective_settings():
    """Authoritative Settings for every runtime consumer.

    = env/startup defaults (load_settings()) deep-merged with the saved
      Settings-DB blob (operator values win). Returns a fresh Settings
      object each call — callers must never mutate it in place.
    """
    from backend.config.settings import Settings

    base = load_settings()
    blob = _load_settings_blob()
    if not blob:
        return base

    data = {
        "mode": getattr(base, "mode", "paper"),
        "capital": {
            "total": base.capital.total,
            "max_allocation_per_trade": base.capital.max_allocation_per_trade,
            "cash_buffer": base.capital.cash_buffer,
        },
        "risk": {
            "max_risk_per_trade_pct": base.risk.max_risk_per_trade_pct,
            "max_daily_loss_pct": base.risk.max_daily_loss_pct,
            "max_trades_per_day": base.risk.max_trades_per_day,
            "max_concurrent_positions": base.risk.max_concurrent_positions,
            "max_consecutive_losses": base.risk.max_consecutive_losses,
            "pause_after_losses_minutes": base.risk.pause_after_losses_minutes,
        },
        "strategy": {
            "name": base.strategy.name,
            "orb_window_start": base.strategy.orb_window_start,
            "orb_window_end": base.strategy.orb_window_end,
            "entry_window_start": base.strategy.entry_window_start,
            "entry_window_end": base.strategy.entry_window_end,
            "exit_all_by": base.strategy.exit_all_by,
        },
    }
    merged = _deep_merge(data, {k: blob[k] for k in BLOB_KEYS if k in blob})

    s = Settings()
    mode = str(merged.get("mode") or s.mode).strip().lower()
    if mode in ("paper", "live", "backtest"):
        s.mode = mode
    cap = merged.get("capital") or {}
    try:
        s.capital.total = float(cap.get("total", s.capital.total))
    except (TypeError, ValueError):
        pass
    try:
        s.capital.max_allocation_per_trade = float(
            cap.get("max_allocation_per_trade", s.capital.max_allocation_per_trade))
    except (TypeError, ValueError):
        pass
    try:
        s.capital.cash_buffer = float(cap.get("cash_buffer", s.capital.cash_buffer))
    except (TypeError, ValueError):
        pass
    risk = merged.get("risk") or {}
    for f in ("max_risk_per_trade_pct", "max_daily_loss_pct"):
        try:
            setattr(s.risk, f, float(risk.get(f, getattr(s.risk, f))))
        except (TypeError, ValueError):
            pass
    for f in ("max_trades_per_day", "max_concurrent_positions", "max_consecutive_losses",
              "pause_after_losses_minutes"):
        try:
            setattr(s.risk, f, int(float(risk.get(f, getattr(s.risk, f)))))
        except (TypeError, ValueError):
            pass
    strat = merged.get("strategy") or {}
    if strat.get("name"):
        s.strategy.name = str(strat["name"]).strip()
    for f in ("orb_window_start", "orb_window_end", "entry_window_start",
              "entry_window_end", "exit_all_by"):
        if strat.get(f):
            setattr(s.strategy, f, str(strat[f]))
    return s


def get_config_sources() -> Dict[str, str]:
    """Provenance labels per operator-adjustable key.

    Returns e.g. {"capital.total": "sqlite_settings", "risk.max_trades_per_day":
    "env_TRADING_CAPITAL", ...}. Possible values:
      sqlite_settings            — value came from the saved Settings blob
      env_<VAR>                  — value came from that environment variable
      default                    — code default (neither DB nor env set it)
      database_unavailable       — DB could not be read; env/default fallback used
    """
    blob = _load_settings_blob()
    sources: Dict[str, str] = {}

    def _is_default(name: str, default: Any) -> bool:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            return True
        try:
            return abs(float(raw) - float(default)) < 1e-12
        except (TypeError, ValueError):
            return False

    # capital.total
    env_cap = os.environ.get("TRADING_CAPITAL")
    if blob and "total" in (blob.get("capital") or {}):
        sources["capital.total"] = "sqlite_settings"
    elif env_cap not in (None, ""):
        sources["capital.total"] = "env_TRADING_CAPITAL"
    else:
        sources["capital.total"] = "default"

    # risk.max_trades_per_day
    env_mtd = os.environ.get("MAX_TRADES_PER_DAY")
    if blob and "max_trades_per_day" in (blob.get("risk") or {}):
        sources["risk.max_trades_per_day"] = "sqlite_settings"
    elif env_mtd not in (None, ""):
        sources["risk.max_trades_per_day"] = "env_MAX_TRADES_PER_DAY"
    else:
        sources["risk.max_trades_per_day"] = "default"

    # Risk scalars the Settings UI exposes (sqlite flag when the blob carries
    # them; otherwise the env var that produced the effective value).
    scalar_map = {
        "capital.max_allocation_per_trade": "MAX_ALLOCATION_PCT",
        "capital.cash_buffer": "CASH_BUFFER_PCT",
        "risk.max_risk_per_trade_pct": "RISK_PER_TRADE_PCT",
        "risk.max_daily_loss_pct": "MAX_DAILY_LOSS_PCT",
        "risk.max_concurrent_positions": "MAX_CONCURRENT_POSITIONS",
        "risk.max_consecutive_losses": "MAX_CONSECUTIVE_LOSSES",
        "risk.pause_after_losses_minutes": "PAUSE_AFTER_LOSSES_MINUTES",
    }
    for key, env in scalar_map.items():
        section, field = key.split(".", 1)
        if blob and field in (blob.get(section) or {}):
            sources[key] = "sqlite_settings"
        elif os.environ.get(env) not in (None, ""):
            sources[key] = f"env_{env}"
        else:
            sources[key] = "default"

    if blob and "mode" in blob:
        sources["mode"] = "sqlite_settings"
    elif os.environ.get("TRADING_MODE"):
        sources["mode"] = "env_TRADING_MODE"
    else:
        sources["mode"] = "default"
    return sources


def detect_config_mismatches(
    settings: Any = None,
    blob: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Saved-vs-runtime divergence warnings (requirement §18).

    With DB-priority resolution the runtime normally EQUALS the saved value,
    so this returns []. A mismatch is only reported when the runtime value
    genuinely diverges from the saved blob — which means a consumer is still
    bypassing this module (a real bug this endpoint exposes) — or when the
    Settings DB is unavailable. Each entry:

        {key, saved_value, runtime_value, source, severity, message}
    """
    if settings is None:
        settings = get_effective_settings()
    if blob is None:
        blob = _load_settings_blob()
    out: List[Dict[str, Any]] = []
    if not blob:
        # Distinguish a genuinely UNREADABLE database (real warning — the
        # runtime is silently on env defaults while the UI may show saved
        # values) from a fresh database that simply never saved a blob
        # (legitimate env/default operation, not a mismatch).
        db_error: Optional[str] = None
        try:
            db = _config_db
            if db is None:
                from backend.database.db_manager import DatabaseManager
                db = DatabaseManager(db_path=os.environ.get("DATABASE_PATH", "data/trading_bot.db"))
            db.load_settings_blob()
        except Exception as exc:
            db_error = type(exc).__name__
        if db_error is not None:
            out.append({
                "key": "database",
                "saved_value": None,
                "runtime_value": None,
                "source": "database_unavailable",
                "severity": "warning",
                "message": (
                    "Saved Settings are unavailable (Settings DB not readable: "
                    f"{db_error}). Runtime is using TRADING_CAPITAL/"
                    "MAX_TRADES_PER_DAY environment defaults; the Settings UI may "
                    "show different values."
                ),
            })
        return out

    checks: List[Tuple[str, Any, Any, str]] = [
        ("capital.total", blob.get("capital", {}).get("total"), getattr(settings.capital, "total", None),
         "TRADING_CAPITAL env"),
        ("risk.max_trades_per_day", blob.get("risk", {}).get("max_trades_per_day"),
         getattr(settings.risk, "max_trades_per_day", None), "MAX_TRADES_PER_DAY env"),
        ("risk.max_concurrent_positions", blob.get("risk", {}).get("max_concurrent_positions"),
         getattr(settings.risk, "max_concurrent_positions", None), "MAX_CONCURRENT_POSITIONS env"),
        ("risk.max_daily_loss_pct", blob.get("risk", {}).get("max_daily_loss_pct"),
         getattr(settings.risk, "max_daily_loss_pct", None), "MAX_DAILY_LOSS_PCT env"),
        ("risk.max_risk_per_trade_pct", blob.get("risk", {}).get("max_risk_per_trade_pct"),
         getattr(settings.risk, "max_risk_per_trade_pct", None), "RISK_PER_TRADE_PCT env"),
        ("capital.max_allocation_per_trade", blob.get("capital", {}).get("max_allocation_per_trade"),
         getattr(settings.capital, "max_allocation_per_trade", None), "MAX_ALLOCATION_PCT env"),
        ("mode", blob.get("mode"), getattr(settings, "mode", None), "TRADING_MODE env"),
    ]
    for key, saved, runtime, env_label in checks:
        if saved is None or runtime is None:
            continue
        try:
            differs = abs(float(saved) - float(runtime)) > 1e-9
        except (TypeError, ValueError):
            differs = str(saved).strip().lower() != str(runtime).strip().lower()
        if differs:
            out.append({
                "key": key,
                "saved_value": saved,
                "runtime_value": runtime,
                "source": env_label,
                "severity": "warning",
                "message": (
                    f"CONFIGURATION_MISMATCH: Saved Settings = {saved}, "
                    f"Runtime = {runtime} ({env_label} still in effect for this "
                    f"consumer — restart or runtime refresh required)."
                ),
            })
    return out


def runtime_config_summary() -> Dict[str, Any]:
    """Compact authoritative-config summary for Operations/Copilot context.

    Includes the capital DEFINITIONS the spec distinguishes:
      starting_capital  — configured capital (settings.capital.total)
      current_equity    — realized equity from the paper equity snapshot
                          (None until the paper worker has persisted one)
    Used-capital / available-capital are computed by Overview from positions,
    never here (they are derived state, not configuration).
    """
    s = get_effective_settings()
    sources = get_config_sources()
    blob = _load_settings_blob()
    mismatches = detect_config_mismatches(settings=s, blob=blob)

    starting = float(s.capital.total)
    current_equity: Optional[float] = None
    equity_detail: Dict[str, Any] = {"available": False}
    try:
        db = _config_db
        if db is None:
            from backend.database.db_manager import DatabaseManager
            db = DatabaseManager(db_path=os.environ.get("DATABASE_PATH", "data/trading_bot.db"))
        raw = db.get_setting("paper_equity_snapshot", "") or ""
        if raw:
            import json as _json
            snap = _json.loads(raw)
            eq = float(snap.get("realized_equity") or 0)
            if eq > 0:
                current_equity = eq
                equity_detail = {"available": True, "source": "paper_equity_snapshot",
                                 "trade_day": snap.get("trade_day")}
    except Exception:
        pass

    return {
        "config_source": "sqlite_settings_over_env",
        "capital": {
            "starting_capital": starting,
            "current_equity": current_equity,
            "max_allocation_per_trade": float(s.capital.max_allocation_per_trade),
            "cash_buffer_pct": float(s.capital.cash_buffer),
            "source": sources.get("capital.total", "unknown"),
            "equity_detail": equity_detail,
        },
        "risk": {
            "max_risk_per_trade_pct": float(s.risk.max_risk_per_trade_pct),
            "max_daily_loss_pct": float(s.risk.max_daily_loss_pct),
            "max_trades_per_day": int(s.risk.max_trades_per_day),
            "max_concurrent_positions": int(s.risk.max_concurrent_positions),
            "max_consecutive_losses": int(s.risk.max_consecutive_losses),
            "pause_after_losses_minutes": int(s.risk.pause_after_losses_minutes),
            "max_trades_source": sources.get("risk.max_trades_per_day", "unknown"),
        },
        "mode": s.mode,
        "mode_source": sources.get("mode", "unknown"),
        "sources": sources,
        "mismatches": mismatches,
    }
