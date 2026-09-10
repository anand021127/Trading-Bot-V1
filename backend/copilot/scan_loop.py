"""PHASE 7 (this pass): connects the Copilot to the EXISTING scanner's
own cadence via `LiveScanner`'s optional `copilot_hook` parameter (see
backend/scanner/live_scanner.py) — not a second loop.

`live_scanner_copilot_hook()` builds a callable with signature
`(symbol, signal, entry) -> None` that `LiveScanner.scan_symbol()` calls
at the end of every symbol's evaluation, at exactly the scanner's own
`seconds_between_symbols` cadence. It reuses the `signal` the scanner
already computed via `evaluate_option_premium()` — it does NOT trigger a
second live chain fetch for the same symbol on the same pass.

BUGFIX (this session): the hook built and validated TradePlans but never
called `submit_trade_plan_for_paper_execution()` — it only logged to the
shadow log, and only in shadow mode. A qualifying, approved TradePlan in
paper mode therefore never became an actual paper position. Fixed below:
in `COPILOT_MODE=paper`, an approved TradePlan is now submitted through
the EXISTING execution pipeline
(execution.py -> TradingEngine.execute_multi_signal -> RiskManager ->
PositionSizer -> OrderManager -> paper fill) — no second execution
engine, no bypass of any existing safety gate. Shadow mode is
unaffected: it still only logs, never executes.

`run_copilot_scan_pass()` is kept for on-demand/manual use (e.g. an API
route or a test) where no LiveScanner instance is driving the cadence.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from backend.copilot.alerts import Alert, AlertStateTracker
from backend.copilot.config import load_copilot_settings
from backend.copilot.decision_engine import build_trade_plan_for_symbol, build_trade_plan_from_signal
from backend.copilot.shadow_logger import log_trade_plan


def _plan_signature(trade_plan: Optional[Dict[str, Any]]) -> Optional[tuple]:
    """A cheap fingerprint of "the same setup" for dedup — same
    instrument, same strike, same entry/stop/target (rounded). If any of
    these actually change, it's a materially different opportunity and
    should be logged/considered again."""
    if not trade_plan:
        return None
    return (
        trade_plan.get("instrument_key"), trade_plan.get("strike"),
        round(trade_plan.get("entry_price_low", 0) or 0, 2), round(trade_plan.get("stop_loss", 0) or 0, 2),
        round(trade_plan.get("target_1", 0) or 0, 2),
    )


def _has_open_position_for_symbol(tools: Any, symbol: str) -> bool:
    """The AUTHORITATIVE duplicate-execution guard: checks the real,
    database-backed open positions (the same source RiskManager/the
    dashboard use), not just in-process memory — so it's correct even
    across restarts and matches how the rest of the system already
    identifies a position (by underlying `symbol`, which is what
    OrderManager's paper fill records — see backend/orders/order_manager.py).
    Fails closed (returns True -> skip execution) if positions can't be
    read at all, since executing blind when position state is unknown
    is the unsafe direction."""
    result = tools.get_open_positions()
    if not result.get("available"):
        return True
    return any(p.get("symbol") == symbol for p in result.get("positions", []))


class CopilotScanState:
    """Per-process dedup + alert state, shared across scan passes. One
    instance should live for the lifetime of the scanner."""

    def __init__(self) -> None:
        self.alert_tracker = AlertStateTracker()
        self._last_logged_signature: Dict[str, Optional[tuple]] = {}

    def should_log(self, symbol: str, trade_plan: Optional[Dict[str, Any]]) -> bool:
        sig = _plan_signature(trade_plan)
        if sig is None:
            return False  # nothing to log (no trade_plan this pass)
        if self._last_logged_signature.get(symbol) == sig:
            return False  # same setup as last time — avoid duplicate records
        self._last_logged_signature[symbol] = sig
        return True


def _process_trade_plan_result(
    tools: Any, symbol: str, result: Dict[str, Any], settings: Any,
    alert_tracker: AlertStateTracker, dedup_state: Optional["CopilotScanState"] = None,
) -> Optional[str]:
    """Shared by BOTH the scanner hook and the manual/batch path
    (`run_copilot_scan_pass`) — one implementation of "what happens to a
    TradePlan result," not two. Handles alerting, shadow logging (dedup
    when `dedup_state` is given), and PAPER execution (always guarded by
    the real open-positions check, so calling this twice for the same
    symbol — from the scanner, from a manual trigger, from a test, in
    any order — can never create a duplicate paper position). Returns
    the formatted alert string, if any, for callers that collect them."""
    decision = result.get("decision", "SKIP") if result.get("available") else "SKIP"
    reason = result.get("reason") or (result.get("analysis") or {}).get("decision_reason", "")

    alert = alert_tracker.check_trade_decision(symbol, decision, reason)

    if settings.mode == "shadow":
        should_log = dedup_state.should_log(symbol, result.get("trade_plan")) if dedup_state else bool(result.get("trade_plan"))
        if should_log:
            try:
                log_trade_plan(result.get("trade_plan"), result.get("validation"), decision)
            except Exception:
                pass
        return alert.formatted() if alert else None

    if settings.mode == "paper" and decision == "TRADE" and result.get("trade_plan") is not None:
        if _has_open_position_for_symbol(tools, symbol):
            return alert.formatted() if alert else None  # already have a position — not a new setup

        from backend.copilot.execution import submit_trade_plan_for_paper_execution
        exec_result = submit_trade_plan_for_paper_execution(
            tools, result["trade_plan"], result["validation"], copilot_settings=settings,
        )
        try:
            log_trade_plan(
                result.get("trade_plan"), result.get("validation"),
                "PAPER_EXECUTED" if exec_result.submitted else f"EXECUTION_REJECTED: {exec_result.reason}",
            )
        except Exception:
            pass

    return alert.formatted() if alert else None


def live_scanner_copilot_hook(tools: Any, state: Optional[CopilotScanState] = None) -> Callable[[str, Any, Any], None]:
    """Returns a callable to pass as `LiveScanner(..., copilot_hook=...)`.
    Reuses the scanner's already-computed `signal` — no duplicate fetch."""
    state = state or CopilotScanState()

    def _hook(symbol: str, signal: Any, scanner_entry: Any) -> None:
        settings = load_copilot_settings()
        if not settings.enabled:
            return

        result = build_trade_plan_from_signal(tools, symbol, signal, analysis=None)
        alert_text = _process_trade_plan_result(tools, symbol, result, settings, state.alert_tracker, dedup_state=state)
        # Alerts are stashed on the entry for whoever reads scanner results
        # next (e.g. an API route) to relay onward — this hook has no
        # direct channel to a UI/notification queue.
        if alert_text is not None and hasattr(scanner_entry, "__dict__"):
            scanner_entry.__dict__.setdefault("copilot_alerts", []).append(alert_text)

    return _hook


def run_copilot_scan_pass(
    tools: Any,
    symbols: List[str],
    alert_tracker: Optional[AlertStateTracker] = None,
    dedup_state: Optional[CopilotScanState] = None,
) -> Dict[str, Any]:
    """On-demand pass over `symbols` for callers NOT driven by a
    LiveScanner instance (e.g. a manual API trigger or a test). Never
    raises — a symbol whose evaluation fails is recorded as an error
    entry, not a crash. Uses the EXACT same `_process_trade_plan_result`
    the scanner hook uses, so a paper execution triggered from here is
    guarded by the same real-open-positions check — calling this
    alongside a running scanner (or repeatedly) can never double-execute
    the same symbol's setup."""
    settings = load_copilot_settings()
    alert_tracker = alert_tracker or AlertStateTracker()

    if not settings.enabled:
        return {"available": False, "reason": "Copilot is disabled (COPILOT_ENABLED=false).", "results": {}, "alerts": []}

    results: Dict[str, Any] = {}
    alerts: List[str] = []

    for symbol in symbols:
        try:
            result = build_trade_plan_for_symbol(tools, symbol)
        except Exception as e:
            result = {"available": False, "reason": f"Unhandled error evaluating {symbol}: {e}"}
        results[symbol] = result

        alert_text = _process_trade_plan_result(tools, symbol, result, settings, alert_tracker, dedup_state=dedup_state)
        if alert_text is not None:
            alerts.append(alert_text)

    return {"available": True, "results": results, "alerts": alerts}
