"""Market-driven V8-D scan → PaperTradingRuntime (paper fills only).

Uses real Upstox candles/chain when a client with a valid token is provided.
Never places live orders — execution is only via PaperTradingRuntime.
Does not modify V8-D strategy parameters.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Protocol, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

# PHASE 5.1 — AI decision layer imports. The gate consumes a structured
# AITradingDecision; it never reaches the broker itself and never replaces
# the hard risk/execution gates that run AFTER it.
from backend.ai_decision.context import MarketSession, RiskContext
from backend.ai_decision.decision_engine import apply_ai_decision_gate
from backend.ai_decision.setup_identity import build_setup_id
from backend.orders.idempotency import make_signal_id


class MarketDataSource(Protocol):
    def get_current_candles(self, symbol: str, interval: str = "5minute", limit: int = 120) -> List[Dict[str, Any]]:
        ...

    def get_nearest_expiry(self, symbol: str) -> Optional[str]:
        ...

    def get_option_chain_with_spot(
        self, symbol: str, expiry_date: str
    ) -> Tuple[List[Dict[str, Any]], Optional[float]]:
        ...


@dataclass
class ScanResult:
    scanned: bool
    traded: bool
    reason: str
    signal: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


def _parse_ts(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    s = str(ts).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _is_trading_session(now: Optional[datetime]) -> bool:
    """Whether `now` (or real time) falls in an open trading session.

    Deterministic when a caller passes an explicit `now` (tests): the date is
    checked against the authoritative calendar, which never consults the wall
    clock for trading-day determination. Production passes None → real time.
    Non-trading days (weekend / official holiday) are market_closed regardless
    of clock time.
    """
    try:
        from backend.market.calendar import exchange_calendar
        dt = now or datetime.now(timezone.utc)
        return exchange_calendar.in_session_hours(dt)
    except Exception:
        # Calendar unavailable: fall back to the exchange session hours in IST
        # (weekday + 09:15–15:30) so the scanner can never hard-crash a worker.
        dt = (now or datetime.now(timezone.utc)).astimezone(IST)
        return dt.weekday() < 5 and (
            dt.replace(hour=9, minute=15, second=0, microsecond=0)
            <= dt <= dt.replace(hour=15, minute=30, second=0, microsecond=0)
        )


def candles_are_fresh(
    candles: List[Dict[str, Any]],
    *,
    max_age_seconds: float = 900.0,
    min_bars: int = 60,
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """Reject empty, short, or stale candle series."""
    if not candles:
        return False, "no_candles"
    if len(candles) < min_bars:
        return False, f"insufficient_candles:{len(candles)}<{min_bars}"
    last = candles[-1]
    for k in ("open", "high", "low", "close"):
        try:
            if float(last.get(k) or 0) <= 0:
                return False, f"invalid_ohlc_{k}"
        except (TypeError, ValueError):
            return False, f"invalid_ohlc_{k}"
    ts = _parse_ts(last.get("timestamp") or last.get("time"))
    if ts is None:
        return False, "missing_candle_timestamp"
    now = now or datetime.now(timezone.utc)
    age = (now - ts.astimezone(timezone.utc)).total_seconds()
    if age > max_age_seconds:
        return False, f"stale_candles_age_sec={int(age)}"
    if age < -120:
        return False, "candle_timestamp_in_future"
    return True, "ok"


def signal_to_paper_payload(sig: Any, expiry: str, quote_age_seconds: float = 1.0) -> Optional[Dict[str, Any]]:
    """Map V8-D StrategySignal → PaperTradingRuntime.submit_entry payload."""
    if getattr(sig, "signal", None) != "BUY":
        return None
    ind = getattr(sig, "indicators", None) or {}
    contract = ind.get("selected_contract") or {}
    ik = contract.get("instrument_key")
    if not ik:
        return None
    lot = int(contract.get("lot_size") or ind.get("lot_size") or 0)
    sizing = ind.get("sizing") or {}
    qty = int(sizing.get("quantity") or sizing.get("qty") or 0)
    if qty <= 0 and lot > 0:
        qty = lot
    if qty <= 0 or lot <= 0:
        return None
    premium = float(getattr(sig, "entry_price", 0) or contract.get("ltp") or 0)
    if premium <= 0:
        return None
    atr = float(contract.get("option_atr") or contract.get("atr") or ind.get("atr") or 0)
    return {
        "timestamp": getattr(sig, "generated_at", None) or datetime.now(timezone.utc).isoformat(),
        "instrument_key": ik,
        "option_type": contract.get("option_type") or ind.get("option_type") or "CE",
        "underlying": getattr(sig, "symbol", "NIFTY50"),
        "strike": float(contract.get("strike") or ind.get("atm_strike") or 0),
        "expiry": expiry or contract.get("expiry") or "",
        "lot_size": lot,
        "premium": premium,
        "spot": float(ind.get("underlying_spot") or ind.get("spot_price") or 0),
        "quantity": qty,
        "stop_loss": float(getattr(sig, "stop_loss", 0) or 0),
        "target": float(getattr(sig, "target", 0) or 0),
        "atr": atr,
        "quote_age_seconds": float(quote_age_seconds),
        "side": "BUY",
    }


def build_scan_signal_id(sig: Any, expiry: str) -> str:
    """Deterministic signal id for the V8-D scan signal — computed with the
    SAME inputs the ExecutionPipeline will use for its own intent id
    (strategy|timestamp|instrument|direction, no extra unique suffix), so
    the durable AI decision and the executed trade share one signal_id and
    "why did the AI approve this?" is answerable from the trade row alone."""
    contract = (getattr(sig, "indicators", None) or {}).get("selected_contract") or {}
    return make_signal_id(
        strategy=str(getattr(sig, "strategy_name", "") or "UNCONFIGURED"),
        timestamp=str(getattr(sig, "generated_at", "") or ""),
        instrument=str(contract.get("instrument_key") or ""),
        direction=str(contract.get("option_type") or getattr(sig, "signal", "")),
    )


# PHASE 5.3 §6: reconciliation verdicts older than this are STALE — no new
# trades until a fresh reconcile resolves state. The worker reconciles every
# 5th tick (~10s), so 15 minutes is generous even across long backfills.
RECONCILE_MAX_AGE_SECONDS = 15 * 60.0


def read_reconciliation_state(runtime: Any) -> tuple:
    """Read the runtime's persisted reconciliation verdict.

    Returns (state, detail_dict, age_seconds):
      state: "1" ok · "0" failed · "" never checked
      age_seconds: seconds since the last reconcile CHECK, or None when the
        verdict carries no timestamp (legacy row / never persisted).
    """
    import json as _json
    try:
        state = str(runtime.db.get_setting("paper_reconcile_ok", "") or "")
    except Exception:
        state = ""
    detail: Dict[str, Any] = {}
    try:
        raw = runtime.db.get_setting("paper_reconcile_detail", "") or ""
        detail = _json.loads(raw) if raw else {}
    except Exception:
        detail = {}
    age: Optional[float] = None
    checked_at = str(detail.get("checked_at") or "")
    if not checked_at:
        try:
            checked_at = str(runtime.db.get_setting("paper_reconcile_checked_at", "") or "")
        except Exception:
            checked_at = ""
    if checked_at:
        try:
            ca = datetime.fromisoformat(checked_at)
            if ca.tzinfo is None:
                ca = ca.replace(tzinfo=timezone.utc)
            age = max(0.0, (datetime.now(timezone.utc) - ca).total_seconds())
        except Exception:
            age = None
    return state, detail, age


class PaperMarketScanner:
    """One scan cycle: candles → V8-D → AI decision → optional paper entry."""

    def __init__(
        self,
        *,
        data: MarketDataSource,
        strategy: Any,
        underlying: str = "NIFTY50",
        interval: str = "5minute",
        max_candle_age_seconds: float = 900.0,
        min_bars: int = 60,
        account_equity: float = 100000.0,
        ai_engine: Optional[Any] = None,
        ai_decision_pipeline_strategy: str = "V8_D_PULLBACK_ATM",
    ) -> None:
        self.data = data
        self.strategy = strategy
        self.underlying = underlying
        self.interval = interval
        self.max_candle_age_seconds = max_candle_age_seconds
        self.min_bars = min_bars
        self.account_equity = account_equity
        # PHASE 5.1: AI decision engine (None or disabled → V8-D-only scan,
        # clearly reported; never a silent fake decision).
        self.ai_engine = ai_engine
        self.ai_decision_pipeline_strategy = ai_decision_pipeline_strategy
        self._last_signal_id: Optional[str] = None

    def scan_once(
        self,
        runtime: Any,
        *,
        trades_today: int = 0,
        kill_switch_active: bool = False,
        now: Optional[datetime] = None,
    ) -> ScanResult:
        """One scan cycle. Delegates to ``_scan_once_impl`` (the unchanged
        market → data → V8-D → AI → risk → paper-execution logic) and ALWAYS
        attaches per-scan diagnostics (session, candle count/freshness,
        expiry, option-chain size, decision, rejection, error) to
        ``ScanResult.details`` so the persisted record can explain exactly
        where the pipeline stopped. Diagnostics never carry secrets."""
        now = now or datetime.now(timezone.utc)
        diag: Dict[str, Any] = {
            "underlying": self.underlying,
            "strategy": str(getattr(self.strategy, "name", "") or
                            getattr(self.strategy, "strategy_name", "") or "V8_D_PULLBACK_ATM"),
            "interval": self.interval,
            "max_candle_age_seconds": self.max_candle_age_seconds,
            "min_bars": self.min_bars,
            "scan_time_ist": now.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        }
        try:
            from backend.market.calendar import exchange_calendar
            _st, _note = exchange_calendar.session_status(now.astimezone(IST))
            diag["session_status"] = _st
            diag["session_note"] = _note
        except Exception:
            diag["session_status"] = "UNKNOWN"
        result = self._scan_once_impl(
            runtime, trades_today=trades_today,
            kill_switch_active=kill_switch_active, now=now, diag=diag,
        )
        merged = dict(diag)
        merged.update(result.details or {})
        result.details = merged
        return result

    def _scan_once_impl(
        self,
        runtime: Any,
        *,
        trades_today: int = 0,
        kill_switch_active: bool = False,
        now: datetime,
        diag: Dict[str, Any],
    ) -> ScanResult:
        # Exchange-calendar gate: never scan on weekends / official NSE/BSE
        # holidays (Muhurat special sessions are honored by the calendar).
        if not _is_trading_session(now):
            return ScanResult(False, False, "market_closed")
        # PHASE 5.3 lifecycle parity: new ENTRIES are allowed only inside the
        # documented session-manager entry window (09:20 entry start → 14:45
        # last-entry cutoff, same rule the backtest engine now enforces).
        # Previously this loop had NO cutoff (any in-session time could open a
        # position — the exact defect that let a 15:25 expiry-day entry into
        # the real backtest data). Positions/square-off/AI monitoring are
        # unaffected: submit_entry and the worker keep running after the
        # cutoff; only NEW entries are gated. Fail-closed on any error.
        try:
            from backend.strategy.session_manager import session_manager as _sm
            _wall = now.astimezone(IST).time() if now.tzinfo else now.time()
            # 09:20 inclusive start, 14:45 INCLUSIVE cutoff (documented spec:
            # 14:45 may still enter, 14:46 cannot) — same boundaries as the
            # backtest engine gate and session_manager.is_entry_window.
            if _wall < _sm.entry_start or _wall > _sm.last_entry:
                return ScanResult(
                    False, False,
                    f"entry_window_closed:{_wall.strftime('%H:%M')}-outside-"
                    f"{_sm.entry_start.strftime('%H:%M')}-{_sm.last_entry.strftime('%H:%M')}",
                )
        except Exception as exc:
            logger.warning("Entry-window check failed (%s) — fail-closed", type(exc).__name__)
            return ScanResult(False, False, f"entry_window_error:{type(exc).__name__}")
        # PHASE 5.2 §5 + 5.3 §6: read the runtime's persisted reconciliation
        # verdict ONCE per scan (with age). "1"=ok, "0"=failed,
        # ""/missing=not yet checked, stale=ok-but-too-old.
        reconciliation_state, rec_detail, rec_age = read_reconciliation_state(runtime)
        if reconciliation_state == "1" and rec_age is not None and rec_age > RECONCILE_MAX_AGE_SECONDS:
            return ScanResult(
                True, False, "AI_NO_TRADE:RECONCILIATION_STALE", signal="BUY",
                details={
                    "reconciliation_age_seconds": round(rec_age, 1),
                    "max_age_seconds": RECONCILE_MAX_AGE_SECONDS,
                    "rejection": ["RECONCILIATION_STALE"],
                },
            )
        # PHASE 5.3 §7: CURRENT equity, never the frozen startup capital.
        # The runtime carries realized P&L-adjusted equity (persisted across
        # restarts); the constructor default is only a last-resort fallback.
        try:
            current_equity = float(getattr(runtime, "realized_equity", 0.0) or 0.0) \
                or float(self.account_equity)
        except (TypeError, ValueError):
            current_equity = float(self.account_equity)
        try:
            candles = self.data.get_current_candles(self.underlying, self.interval, limit=120)
        except Exception as exc:
            logger.warning("Candle fetch failed: %s", type(exc).__name__)
            from backend.paper.scan_state import describe_exception
            diag["error"] = describe_exception(exc)
            # scanned=True: the scan EXECUTED and reached the market-data
            # stage; the data call failed. (scanned=False is reserved for
            # "did not run" — market closed / not attempted.)
            return ScanResult(True, False, f"candle_fetch_error:{type(exc).__name__}")

        diag["candle_count"] = len(candles or [])
        try:
            _last = (candles or [])[-1] if candles else None
            _lts = _parse_ts((_last or {}).get("timestamp") or (_last or {}).get("time"))
            if _lts is not None:
                diag["last_candle_ts"] = _lts.astimezone(IST).isoformat()
                diag["candle_age_seconds"] = round((now - _lts.astimezone(timezone.utc)).total_seconds(), 1)
        except Exception:
            pass
        ok, reason = candles_are_fresh(
            candles,
            max_age_seconds=self.max_candle_age_seconds,
            min_bars=self.min_bars,
            now=now,
        )
        if not ok:
            return ScanResult(True, False, reason, details={"bars": len(candles or [])})

        try:
            expiry = self.data.get_nearest_expiry(self.underlying)
        except Exception as exc:
            from backend.paper.scan_state import describe_exception
            diag["error"] = describe_exception(exc)
            return ScanResult(True, False, f"expiry_fetch_error:{type(exc).__name__}")
        if not expiry:
            return ScanResult(True, False, "no_upcoming_expiry")
        diag["expiry"] = str(expiry)

        try:
            chain, chain_spot = self.data.get_option_chain_with_spot(self.underlying, expiry)
        except Exception as exc:
            from backend.paper.scan_state import describe_exception
            diag["error"] = describe_exception(exc)
            return ScanResult(True, False, f"chain_fetch_error:{type(exc).__name__}")
        diag["option_chain_count"] = len(chain or [])
        if not chain:
            return ScanResult(True, False, "empty_option_chain")

        spot = float(chain_spot or 0)
        if spot <= 0:
            spot = float(candles[-1].get("close") or 0)
        if spot <= 0:
            return ScanResult(True, False, "no_valid_spot")
        diag["spot"] = round(spot, 2)

        last_ts = _parse_ts(candles[-1].get("timestamp"))
        quote_age = (now - last_ts.astimezone(timezone.utc)).total_seconds() if last_ts else 0.0

        try:
            sig, decision_log = self.strategy.evaluate_v8d_signal(
                underlying_symbol=self.underlying,
                underlying_candles=candles,
                spot_price=spot,
                option_chain=chain,
                account_equity=current_equity,
                trades_today=trades_today,
                kill_switch_active=kill_switch_active,
                # PHASE 5.2 §5: real reconciliation state, never a hardcoded
                # True. "1" = ok; "0" or unknown/pending = NOT ok (the V8-D
                # strategy itself rejects on mismatch or pending).
                reconciliation_ok=(reconciliation_state == "1"),
            )
        except Exception as exc:
            logger.exception("V8-D evaluate failed")
            from backend.paper.scan_state import describe_exception
            diag["error"] = describe_exception(exc)
            return ScanResult(True, False, f"strategy_error:{type(exc).__name__}")

        decision = getattr(decision_log, "decision", None) or "NONE"
        diag["decision"] = str(decision)
        diag["v8d_evaluated"] = True
        try:
            _c = ((getattr(sig, "indicators", None) or {}).get("selected_contract") or {})
            if _c:
                diag["selected_contract"] = {
                    k: _c.get(k) for k in ("instrument_key", "strike", "option_type",
                                            "lot_size", "ltp", "expiry") if k in _c
                }
        except Exception:
            pass
        if getattr(sig, "signal", None) != "BUY":
            return ScanResult(
                True,
                False,
                f"no_trade:{decision}",
                signal=None,
                details={
                    "rejection": list(getattr(sig, "rejected_reasons", None) or []),
                    "decision": decision,
                },
            )

        payload = signal_to_paper_payload(sig, expiry=expiry, quote_age_seconds=quote_age)
        if not payload:
            return ScanResult(True, False, "signal_payload_incomplete", signal="BUY")

        # PHASE 5.3 QA: per-underlying duplicate-position guard. The engine and
        # the live path both refuse a second position for the SAME underlying
        # ("POSITION_ALREADY_OPEN"); the paper broker only enforces per-
        # instrument-key uniqueness, so a second different contract for the same
        # underlying could previously stack exposure. Fail-closed, cheap, and
        # consistent with the backtest/live semantics.
        try:
            _scan_underlying = str(payload.get("underlying") or getattr(sig, "symbol", "") or "").upper()
            for _p in getattr(runtime.broker, "positions", {}).values():
                if int(_p.get("quantity") or 0) == 0:
                    continue
                if str(_p.get("underlying") or "").upper() == _scan_underlying:
                    return ScanResult(
                        True, False,
                        f"POSITION_ALREADY_OPEN — Position already active for {_scan_underlying}",
                        signal="BUY",
                        details={"rejection": ["POSITION_ALREADY_OPEN"]},
                    )
        except Exception as exc:
            logger.warning("Duplicate-position check failed (%s) — fail-closed", type(exc).__name__)
            return ScanResult(True, False, f"position_check_error:{type(exc).__name__}", signal="BUY")

        # Ensure contract carries expiry for validator
        payload["expiry"] = expiry

        # ── AI TRADING DECISION gate (PHASE 5.1 §6, 5.3 §8) ───────────
        # V8-D produced a BUY signal; the AI decision layer now evaluates it
        # BEFORE the hard risk/execution gates. REJECT/WAIT/provider failure
        # here means NO paper order regardless of the V8-D signal. The gate
        # only consumes the structured AITradingDecision — it never calls
        # the broker — and hard risk still runs after it inside
        # runtime.submit_entry (kill switch → pipeline → sizer).
        #
        # PHASE 5.3 §8: the operator toggle (control API → DB override)
        # wins over the env-configured default and is re-read EVERY scan,
        # so the UI switch takes effect within one tick without a restart.
        ai_on = False
        if self.ai_engine is not None:
            try:
                override = str(runtime.db.get_setting("ai_decision_enabled_override", "") or "")
            except Exception:
                override = ""
            if override == "1":
                ai_on = True
            elif override == "0":
                ai_on = False
            else:
                ai_on = bool(getattr(self.ai_engine, "enabled", False))
        if self.ai_engine is not None and ai_on:
            contract = (getattr(sig, "indicators", None) or {}).get("selected_contract") or {}
            try:
                kill_level = runtime.kill.level()
            except Exception:
                kill_level = "UNKNOWN"

            # PHASE 5.2 §5: REAL reconciliation state — read from the
            # runtime's own last reconcile() verdict (persisted by the
            # worker every 5th tick), never a hardcoded True.
            rec_ok: Optional[bool]
            if reconciliation_state == "1":
                rec_ok = True
            elif reconciliation_state == "0":
                rec_ok = False
            else:
                rec_ok = None  # reconciliation not yet run this session

            # Reconciliation failure → do NOT call the AI at all. The final
            # decision is a typed NO-TRADE with RECONCILIATION_NOT_READY.
            if rec_ok is False:
                return ScanResult(
                    True, False, "AI_NO_TRADE:RECONCILIATION_NOT_READY", signal="BUY",
                    details={
                        "ai_decision": "REJECT",
                        "ai_reason_codes": ["RECONCILIATION_NOT_READY"],
                        "rejection": list(getattr(sig, "rejected_reasons", None) or []),
                    },
                )

            risk_ctx = RiskContext(
                equity=float(getattr(runtime, "realized_equity", 0.0) or 0.0),
                open_positions=len([
                    p for p in getattr(runtime.broker, "positions", {}).values()
                    if int(p.get("quantity") or 0) != 0
                ]),
                trades_today=int(getattr(runtime, "trades_today", 0) or 0),
                daily_realized_pnl=float(getattr(runtime, "daily_realized_pnl", 0.0) or 0.0),
                kill_switch=bool(kill_switch_active) or kill_level != "OFF",
                kill_switch_level=str(kill_level),
                reconciliation_ok=rec_ok,
            )
            session = MarketSession(open=True, is_trading_day=True, label="PAPER_SCAN")
            sig_id = build_scan_signal_id(sig, expiry)
            self._last_signal_id = sig_id
            # PHASE 5.3 §12/§13: bounded-blocking decide. The scan never
            # holds a live signal longer than AI_DECISION_BUDGET_SECONDS
            # (≤ provider timeout): a slow decision becomes typed WAIT this
            # tick and resolves via dedup-replay on a later scan — the
            # signal is never traded on stale data while the model thinks.
            try:
                budget = float(os.environ.get("AI_DECISION_BUDGET_SECONDS", "10"))
            except (TypeError, ValueError):
                budget = 10.0
            ai_decision = self.ai_engine.decide_with_budget(
                max_wait_seconds=budget,
                signal_id=sig_id,
                setup_id=build_setup_id(signal=sig, contract=contract, expiry=expiry, candles=candles),
                signal=sig,
                contract=contract,
                expiry=expiry,
                candles=candles,
                candles_fresh=ok,
                candle_age_seconds=quote_age,
                risk=risk_ctx,
                session=session,
                pipeline_strategy=self.ai_decision_pipeline_strategy,
            )
            ai_reason = apply_ai_decision_gate(
                payload, ai_decision, pipeline_strategy=self.ai_decision_pipeline_strategy,
            )
            if ai_reason is not None:
                return ScanResult(
                    True, False, ai_reason, signal="BUY",
                    details={
                        "ai_decision": ai_decision.decision,
                        "ai_confidence": ai_decision.confidence,
                        "ai_reason_codes": list(ai_decision.reason_codes),
                        "ai_decision_id": ai_decision.decision_id,
                        "ai_model": f"{ai_decision.model_provider}/{ai_decision.model_name}",
                        "rejection": list(getattr(sig, "rejected_reasons", None) or []),
                    },
                )

        try:
            result = runtime.submit_entry(payload)
        except Exception as exc:
            logger.exception("Paper submit failed")
            from backend.paper.scan_state import describe_exception
            diag["error"] = describe_exception(exc)
            return ScanResult(True, False, f"submit_error:{type(exc).__name__}", signal="BUY")

        accepted = bool(getattr(result, "accepted", False))
        return ScanResult(
            True,
            accepted,
            getattr(result, "reason", "submitted") if accepted else f"rejected:{getattr(result, 'reason', '')}",
            signal="BUY",
            details={
                "signal_id": getattr(result, "signal_id", None),
                "instrument_key": payload.get("instrument_key"),
                "premium": payload.get("premium"),
                "quantity": payload.get("quantity"),
                "ai_decision": (payload.get("ai_decision") or {}).get("decision"),
                "ai_decision_id": (payload.get("ai_decision") or {}).get("decision_id"),
                "ai_confidence": (payload.get("ai_decision") or {}).get("confidence"),
            },
        )


class UpstoxMarketDataSource:
    """Thin adapter over UpstoxClient — real API only, no synthetic candles."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def get_current_candles(self, symbol: str, interval: str = "5minute", limit: int = 120) -> List[Dict[str, Any]]:
        if hasattr(self.client, "get_current_candles"):
            return self.client.get_current_candles(symbol, interval=interval, limit=limit) or []
        # Fallback: historical + intraday merge not available
        if hasattr(self.client, "get_intraday_candles"):
            return self.client.get_intraday_candles(symbol, interval) or []
        return []

    def get_nearest_expiry(self, symbol: str) -> Optional[str]:
        return self.client.get_nearest_expiry(symbol)

    def get_option_chain_with_spot(
        self, symbol: str, expiry_date: str
    ) -> Tuple[List[Dict[str, Any]], Optional[float]]:
        if hasattr(self.client, "get_option_chain_with_spot"):
            return self.client.get_option_chain_with_spot(symbol, expiry_date)
        chain = self.client.get_option_chain(symbol, expiry_date) or []
        return chain, None
