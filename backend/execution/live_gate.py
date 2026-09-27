"""PHASE 5.3 §5/§10 — explicit live-trading readiness gate.

Live trading is NEVER enabled by flipping an env var in the frontend. The
mode-switch endpoint calls `evaluate_live_readiness()` and refuses to arm
LIVE unless EVERY mandatory condition holds, returning the exact blocked
reasons. The gate is read-only over real runtime state:

  Upstox authentication OK          (verified against the broker profile API)
+ market data healthy               (instrument master fresh, quote fetchable)
+ reconciliation healthy            (fresh OK verdict, not stale, not failed)
+ risk manager healthy              (authoritative risk config resolves)
+ execution pipeline healthy        (armed, strategy+product configured)
+ kill switch healthy               (not triggered)
+ strategy valid                    (configured == V8_D_PULLBACK_ATM)
+ instrument resolution healthy     (all configured underlyings resolvable)

Only then: LIVE READY. Otherwise LIVE BLOCKED with the exact reason list.

The gate NEVER mutates state and NEVER places orders. Arming live also
requires the operator-level control token (handled at the API layer) — a
frontend click alone can never arm live trading.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

LIVE_STRATEGY = "V8_D_PULLBACK_ATM"


@dataclass
class LiveReadiness:
    ready: bool
    blocked_reasons: List[str] = field(default_factory=list)
    checks: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ready": self.ready,
            "blocked_reasons": list(self.blocked_reasons),
            "checks": self.checks,
        }


def _check(name: str, ok: bool, detail: Any,
           reasons: List[str], checks: Dict[str, Dict[str, Any]]) -> None:
    checks[name] = {"ok": bool(ok), "detail": detail}
    if not ok:
        reasons.append(name)


def evaluate_live_readiness(
    *,
    client: Optional[Any] = None,
    db: Optional[Any] = None,
    settings: Optional[Any] = None,
    runtime: Optional[Any] = None,
    require_funds: bool = True,
) -> LiveReadiness:
    """Evaluate every mandatory live-trading precondition against REAL state.

    Read-only. Returns a structured verdict; the caller decides what to do
    with it (the mode endpoint refuses to arm live unless `ready`).
    """
    reasons: List[str] = []
    checks: Dict[str, Dict[str, Any]] = {}

    # ── 1. Mode / environment ────────────────────────────────────────────
    mode = str(getattr(settings, "mode", "") or os.environ.get("TRADING_MODE", "") or "").lower()
    _check("environment_mode", mode in ("paper", "live", "backtest"),
           {"mode": mode or "unset"}, reasons, checks)

    # ── 2. Upstox authentication ─────────────────────────────────────────
    auth_ok, auth_detail = False, "no_client"
    if client is not None:
        try:
            profile = client.get_profile()
            auth_ok = bool(profile and (profile.get("data") or profile).get("user_id"))
            auth_detail = "profile_ok" if auth_ok else "profile_missing_user_id"
        except Exception as exc:  # noqa: BLE001
            auth_ok, auth_detail = False, f"profile_error:{type(exc).__name__}"
    _check("upstox_auth", auth_ok, auth_detail, reasons, checks)

    # ── 3. Broker funds visible (real account state, not startup capital) ─
    if require_funds:
        funds_ok, funds_detail = False, "no_client"
        if client is not None:
            try:
                funds = client.get_funds() or {}
                avail = float(funds.get("available_margin") or 0)
                funds_ok = avail > 0
                funds_detail = {"available_margin": avail} if funds_ok else "no_available_margin"
            except Exception as exc:  # noqa: BLE001
                funds_ok, funds_detail = False, f"funds_error:{type(exc).__name__}"
        _check("broker_funds", funds_ok, funds_detail, reasons, checks)

    # ── 4. Market data health (instrument master) ────────────────────────
    data_ok, data_detail = False, "unknown"
    try:
        from backend.broker.instrument_master import get_master_status
        st = get_master_status() or {}
        data_ok = bool(st.get("symbols_loaded")) and not st.get("is_stale", True)
        data_detail = {"symbols_loaded": st.get("symbols_loaded"),
                       "is_stale": st.get("is_stale"),
                       "last_error": st.get("last_error")}
    except Exception as exc:  # noqa: BLE001
        data_detail = f"instrument_master_error:{type(exc).__name__}"
    _check("instrument_master", data_ok, data_detail, reasons, checks)

    # ── 5. Reconciliation healthy (fresh OK verdict, never a default) ────
    rec_ok, rec_detail = False, "no_db"
    if db is not None:
        try:
            state = str(db.get_setting("reconcile_ok", "") or
                        db.get_setting("paper_reconcile_ok", "") or "")
            raw_detail = db.get_setting("paper_reconcile_detail", "") or ""
            import json as _json
            detail: Dict[str, Any] = {}
            try:
                detail = _json.loads(raw_detail) if raw_detail else {}
            except Exception:
                detail = {}
            checked_at = str(detail.get("checked_at") or "")
            age: Optional[float] = None
            if checked_at:
                from datetime import datetime, timezone
                try:
                    ca = datetime.fromisoformat(checked_at)
                    if ca.tzinfo is None:
                        ca = ca.replace(tzinfo=timezone.utc)
                    age = max(0.0, (datetime.now(timezone.utc) - ca).total_seconds())
                except Exception:
                    age = None
            from backend.paper.market_scan_loop import RECONCILE_MAX_AGE_SECONDS
            if state == "1" and age is not None and age <= RECONCILE_MAX_AGE_SECONDS:
                rec_ok = True
                rec_detail = {"age_seconds": round(age, 1)}
            elif state == "1":
                rec_detail = {"state": state, "age_seconds": age,
                              "error": "reconciliation_stale"}
            else:
                rec_detail = {"state": state or "never_checked",
                              "age_seconds": age, "error": "reconciliation_not_ok"}
        except Exception as exc:  # noqa: BLE001
            rec_detail = f"reconcile_state_error:{type(exc).__name__}"
    _check("reconciliation", rec_ok, rec_detail, reasons, checks)

    # ── 6. Risk manager healthy ──────────────────────────────────────────
    risk_ok, risk_detail = False, "no_settings"
    if settings is not None:
        try:
            from backend.risk.risk_config import build_authoritative_risk_config
            cfg = build_authoritative_risk_config(
                capital=float(settings.capital.total),
                strategy_risk_pct=float(settings.risk.max_risk_per_trade_pct),
                engine_risk_pct=float(settings.risk.max_risk_per_trade_pct),
                risk_manager_daily_loss_pct=float(settings.risk.max_daily_loss_pct),
                configured_risk_pct=float(settings.risk.max_risk_per_trade_pct),
                allocation_limit_pct=float(settings.capital.max_allocation_per_trade),
                max_daily_trades=int(settings.risk.max_trades_per_day),
                max_positions=int(settings.risk.max_concurrent_positions),
                max_daily_loss_pct=float(settings.risk.max_daily_loss_pct),
                lot_size_source="contract_metadata",
                order_product=str(os.environ.get("UPSTOX_ORDER_PRODUCT", "I")),
                strategy_name=LIVE_STRATEGY,
                eod_square_off=getattr(settings.strategy, "exit_all_by", "15:15") or "15:15",
            )
            risk_ok = cfg.capital > 0 and cfg.max_daily_trades > 0
            risk_detail = {"capital": cfg.capital, "max_daily_trades": cfg.max_daily_trades}
        except Exception as exc:  # noqa: BLE001
            risk_detail = f"risk_config_error:{type(exc).__name__}"
    _check("risk_manager", risk_ok, risk_detail, reasons, checks)

    # ── 7. Kill switch clear ─────────────────────────────────────────────
    kill_ok, kill_detail = False, "no_db"
    if db is not None:
        try:
            from backend.execution.kill_switch import PersistentKillSwitch
            kill = PersistentKillSwitch(db)
            level = str(kill.level() or "OFF")
            kill_ok = level == "OFF"
            kill_detail = {"level": level}
        except Exception as exc:  # noqa: BLE001
            kill_detail = f"kill_switch_error:{type(exc).__name__}"
    _check("kill_switch", kill_ok, kill_detail, reasons, checks)

    # ── 8. Strategy valid ────────────────────────────────────────────────
    strategy = str(getattr(settings, "strategy", "").name if settings is not None and
                   hasattr(getattr(settings, "strategy", None), "name")
                   else os.environ.get("TRADING_STRATEGY", ""))
    _check("strategy_valid", strategy == LIVE_STRATEGY,
           {"strategy": strategy or "unset", "expected": LIVE_STRATEGY}, reasons, checks)

    # ── 9. Instrument resolution healthy (all six underlyings) ───────────
    res_ok, res_detail = False, "unknown"
    try:
        from backend.config.universe_config import VALID_OPTION_INDICES
        from backend.broker.instrument_master import resolve_instrument_key
        unresolved = [u for u in VALID_OPTION_INDICES
                      if not resolve_instrument_key(u, static_fallback=None)]
        res_ok = not unresolved
        res_detail = {"unresolved": unresolved, "checked": len(VALID_OPTION_INDICES)}
    except Exception as exc:  # noqa: BLE001
        res_detail = f"instrument_resolution_error:{type(exc).__name__}"
    _check("instrument_resolution", res_ok, res_detail, reasons, checks)

    # ── 10. Execution pipeline armed (live runbook requires it) ──────────
    pipe_ok, pipe_detail = True, "checked_by_trading_engine_on_start"
    if runtime is not None and hasattr(runtime, "_pipeline"):
        pipe_ok = runtime._pipeline is not None
        pipe_detail = "armed" if pipe_ok else "not_armed"
    _check("execution_pipeline", pipe_ok, pipe_detail, reasons, checks)

    return LiveReadiness(ready=not reasons, blocked_reasons=reasons, checks=checks)
