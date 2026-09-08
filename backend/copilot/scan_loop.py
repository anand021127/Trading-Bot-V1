"""PHASE 7 (this pass): connects the Copilot to the EXISTING scanner's
own cadence via `LiveScanner`'s new optional `copilot_hook` parameter
(see backend/scanner/live_scanner.py) — not a second loop.

`live_scanner_copilot_hook()` builds a callable with signature
`(symbol, signal, entry) -> None` that `LiveScanner.scan_symbol()` calls
at the end of every symbol's evaluation, at exactly the scanner's own
`seconds_between_symbols` cadence. It reuses the `signal` the scanner
already computed via `evaluate_option_premium()` — it does NOT trigger a
second live chain fetch for the same symbol on the same pass.

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
    should be logged again."""
    if not trade_plan:
        return None
    return (
        trade_plan.get("instrument_key"), trade_plan.get("strike"),
        round(trade_plan.get("entry_price_low", 0) or 0, 2), round(trade_plan.get("stop_loss", 0) or 0, 2),
        round(trade_plan.get("target_1", 0) or 0, 2),
    )


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


def live_scanner_copilot_hook(tools: Any, state: Optional[CopilotScanState] = None) -> Callable[[str, Any, Any], None]:
    """Returns a callable to pass as `LiveScanner(..., copilot_hook=...)`.
    Reuses the scanner's already-computed `signal` — no duplicate fetch."""
    state = state or CopilotScanState()

    def _hook(symbol: str, signal: Any, scanner_entry: Any) -> None:
        settings = load_copilot_settings()
        if not settings.enabled:
            return

        result = build_trade_plan_from_signal(tools, symbol, signal, analysis=None)
        decision = result.get("decision", "SKIP") if result.get("available") else "SKIP"
        reason = result.get("reason") or ""

        alert = state.alert_tracker.check_trade_decision(symbol, decision, reason)
        # Alerts are stashed on the entry for whoever reads scanner results
        # next (e.g. an API route) to relay onward — this hook has no
        # direct channel to a UI/notification queue.
        if alert is not None and hasattr(scanner_entry, "__dict__"):
            scanner_entry.__dict__.setdefault("copilot_alerts", []).append(alert.formatted())

        if settings.mode == "shadow" and state.should_log(symbol, result.get("trade_plan")):
            try:
                log_trade_plan(result.get("trade_plan"), result.get("validation"), decision)
            except Exception:
                pass  # shadow logging must never break the scanner

    return _hook


def run_copilot_scan_pass(
    tools: Any,
    symbols: List[str],
    alert_tracker: Optional[AlertStateTracker] = None,
) -> Dict[str, Any]:
    """On-demand pass over `symbols` for callers NOT driven by a
    LiveScanner instance (e.g. a manual API trigger or a test). Never
    raises — a symbol whose evaluation fails is recorded as an error
    entry, not a crash."""
    settings = load_copilot_settings()
    alert_tracker = alert_tracker or AlertStateTracker()

    if not settings.enabled:
        return {"available": False, "reason": "Copilot is disabled (COPILOT_ENABLED=false).", "results": {}, "alerts": []}

    results: Dict[str, Any] = {}
    alerts: List[Alert] = []

    for symbol in symbols:
        try:
            result = build_trade_plan_for_symbol(tools, symbol)
        except Exception as e:
            result = {"available": False, "reason": f"Unhandled error evaluating {symbol}: {e}"}
        results[symbol] = result

        decision = result.get("decision", "SKIP") if result.get("available") else "SKIP"
        reason = result.get("reason") or (result.get("analysis") or {}).get("decision_reason", "")
        alert = alert_tracker.check_trade_decision(symbol, decision, reason)
        if alert is not None:
            alerts.append(alert)

        if settings.mode == "shadow" and result.get("trade_plan") is not None:
            try:
                log_trade_plan(result["trade_plan"], result["validation"], decision)
            except Exception:
                pass

    return {"available": True, "results": results, "alerts": [a.formatted() for a in alerts]}
