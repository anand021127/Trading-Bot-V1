"""Live Scanner — item #3.

Runs continuously in the background (independent of whether the bot is
actually placing trades) so the dashboard can show exactly what's being
analyzed right now: symbol, LTP, indicator status, and a plain-English
decision — including every rejection reason. Nothing here is faked; if a
symbol can't be evaluated (no data, API error), it shows up with an
explicit error, not a fabricated PASS/FAIL.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Configurable: scanner is considered unhealthy if no successful scan
# within this many seconds.
SCANNER_HEALTH_TIMEOUT_SECONDS = 60


@dataclass
class ScannerEntry:
    symbol: str
    ltp: Optional[float] = None
    scanned_at: str = ""
    ema_status: str = "N/A"
    rsi_value: Optional[float] = None
    rsi_status: str = "N/A"
    atr: Optional[float] = None
    volume_status: str = "N/A"
    trend: str = "NEUTRAL"
    decision: str = ""
    signal: str = "NONE"
    confidence: float = 0.0
    rejected_reasons: List[str] = field(default_factory=list)
    strategy_breakdown: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    strategy_name: str = ""
    contract_resolution_status: str = "N/A"
    execution_status: str = "SIGNAL_ONLY"
    execution_reason: str = ""
    instrument_key: str = ""
    strike: Optional[float] = None
    expiry: str = ""
    option_type: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "ltp": self.ltp,
            "scanned_at": self.scanned_at,
            "ema_status": self.ema_status,
            "rsi_value": self.rsi_value,
            "rsi_status": self.rsi_status,
            "atr": self.atr,
            "volume_status": self.volume_status,
            "trend": self.trend,
            "decision": self.decision,
            "signal": self.signal,
            "confidence": self.confidence,
            "rejected_reasons": self.rejected_reasons,
            "strategy_breakdown": self.strategy_breakdown,
            "error": self.error,
            "strategy_name": self.strategy_name,
            "contract_resolution_status": self.contract_resolution_status,
            "execution_status": self.execution_status,
            "execution_reason": self.execution_reason,
            "instrument_key": self.instrument_key,
            "strike": self.strike,
            "expiry": self.expiry,
            "option_type": self.option_type,
        }


def _status(passed: Optional[bool]) -> str:
    if passed is None:
        return "N/A"
    return "PASS" if passed else "FAILED"


class LiveScanner:
    """Iterates the configured universe one symbol at a time, evaluating the
    multi-strategy engine for each, and keeps the latest result + "currently
    scanning" pointer available for the dashboard.
    """

    def __init__(
        self,
        trading_engine: Any,
        universe_resolver: Any,
        seconds_between_symbols: float = 3.0,
        mode_resolver: Optional[Any] = None,
        copilot_hook: Optional[Any] = None,
    ) -> None:
        self.trading_engine = trading_engine
        self.universe_resolver = universe_resolver  # callable -> List[str]
        self.mode_resolver = mode_resolver or (lambda: "OPTIONS")
        self.seconds_between_symbols = seconds_between_symbols
        # Optional callable(symbol: str, signal: StrategySignal, entry: ScannerEntry) -> None,
        # invoked at the end of each scan_symbol() call — i.e. at exactly
        # this scanner's own cadence, not a second competing loop. Default
        # None means zero behavior change from before this parameter
        # existed. See backend/copilot/scan_loop.py:live_scanner_copilot_hook
        # for the Copilot's implementation of this callable.
        self.copilot_hook = copilot_hook

        self._results: Dict[str, ScannerEntry] = {}
        self._results_lock = threading.Lock()
        self.currently_scanning: Optional[str] = None
        self.is_running: bool = False
        self.last_full_pass_completed_at: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._should_run = False

        # ── heartbeat / health tracking ──────────────────────────────────
        self._last_scan_mono: float = 0.0
        self._last_scan_duration: float = 0.0
        self._scan_count: int = 0
        self._consecutive_scan_failures: int = 0
        self._last_scan_error: Optional[str] = None
        self._started_at_mono: float = 0.0
        self._cached_watching_count: int = 0

    # ── one symbol / one pass — synchronous, directly testable ───────────

    def scan_symbol(self, symbol: str) -> ScannerEntry:
        self.currently_scanning = symbol
        entry = ScannerEntry(symbol=symbol, scanned_at=datetime.now(timezone.utc).isoformat())

        try:
            from backend.api.websocket import get_prices_by_symbol
            live_prices = get_prices_by_symbol()
            tick = live_prices.get(symbol)
            if tick:
                entry.ltp = tick.get("ltp")
        except Exception:
            pass

        try:
            mode = self.mode_resolver()
        except Exception:
            mode = "OPTIONS"

        try:
            # Instance override wins (tests). Class-defined method on a real
            # engine wins over MagicMock auto-attributes.
            _eng = self.trading_engine
            _inst_dict = getattr(_eng, "__dict__", {})
            if "evaluate_configured_strategy" in _inst_dict and callable(_inst_dict["evaluate_configured_strategy"]):
                best = _eng.evaluate_configured_strategy(symbol)
            elif "evaluate_configured_strategy" in type(_eng).__dict__:
                best = _eng.evaluate_configured_strategy(symbol)
            else:
                best = _eng.evaluate_option_premium(symbol)
            signals = [best]
        except Exception as e:
            entry.error = str(e)
            entry.decision = f"ERROR — {e}"
            entry.execution_status = "BLOCKED"
            entry.execution_reason = str(e)
            with self._results_lock:
                self._results[symbol] = entry
            return entry

        entry.strategy_breakdown = [s.to_dict() for s in signals if hasattr(s, "to_dict")]
        best = signals[0] if signals else None
        if best is not None:
            entry.strategy_name = getattr(best, "strategy_name", "") or ""
            entry.decision = getattr(best, "entry_reason", "") or ""
            entry.signal = getattr(best, "signal", "NONE") or "NONE"
            entry.confidence = float(getattr(best, "confidence", 0) or 0)
            entry.rejected_reasons = list(getattr(best, "rejected_reasons", None) or [])
            ind = getattr(best, "indicators", None) or {}
            contract = ind.get("selected_contract") if isinstance(ind, dict) else None
            if isinstance(contract, dict):
                entry.instrument_key = str(contract.get("instrument_key") or "")
                entry.strike = contract.get("strike")
                entry.expiry = str(contract.get("expiry") or ind.get("expiry") or "")
                entry.option_type = str(contract.get("option_type") or "")
                entry.contract_resolution_status = "RESOLVED" if entry.instrument_key else "FAILED"
                if entry.option_type == "CE":
                    entry.trend = "BULLISH"
                elif entry.option_type == "PE":
                    entry.trend = "BEARISH"
            else:
                # Contract missing — classify resolution from rejection text
                joined = " ".join(entry.rejected_reasons).lower()
                if "could not resolve" in joined or "atm" in joined:
                    entry.contract_resolution_status = "FAILED"
                elif entry.signal == "BUY":
                    entry.contract_resolution_status = "FAILED"
                else:
                    entry.contract_resolution_status = "N/A"
            if not entry.ltp:
                entry.ltp = getattr(best, "entry_price", None) or entry.ltp
            if isinstance(ind, dict):
                entry.rsi_value = ind.get("rsi")
                entry.atr = ind.get("atr")
                if not entry.expiry:
                    entry.expiry = str(ind.get("expiry") or "")

            # Paper execution: only when bot is started AND signal is BUY
            entry.execution_status = "SIGNAL_ONLY"
            entry.execution_reason = ""
            if entry.signal == "BUY":
                entry = self._maybe_submit_paper_entry(entry, best)

        with self._results_lock:
            self._results[symbol] = entry

        if self.copilot_hook is not None:
            try:
                self.copilot_hook(symbol, best, entry)
            except Exception as e:
                logger.warning("copilot_hook raised for %s: %s", symbol, e)

        return entry


    def _maybe_submit_paper_entry(self, entry: ScannerEntry, sig: Any) -> ScannerEntry:
        """Submit BUY to canonical PaperTradingRuntime when bot is running in paper mode."""
        try:
            from backend.config.settings import load_settings
            settings = load_settings()
            if (settings.mode or "").lower() != "paper":
                entry.execution_status = "BLOCKED"
                entry.execution_reason = "not_paper_mode"
                return entry
            if (getattr(settings.strategy, "name", "") or "").strip() != "V8_D_PULLBACK_ATM":
                entry.execution_status = "BLOCKED"
                entry.execution_reason = "strategy_not_v8d"
                return entry
        except Exception as e:
            entry.execution_status = "BLOCKED"
            entry.execution_reason = f"settings_error:{e}"
            return entry

        try:
            from backend.api.routers import bot_control
            if not bot_control.BotState.is_running():
                entry.execution_status = "SIGNAL_ONLY"
                entry.execution_reason = "bot_not_started"
                entry.decision = (entry.decision or "") + " | EXECUTION: bot not started (signal only)"
                return entry
            if bot_control.BotState.status().get("kill_switch_active"):
                entry.execution_status = "BLOCKED"
                entry.execution_reason = "kill_switch_active"
                return entry
            runtime = bot_control.get_paper_runtime()
        except Exception as e:
            entry.execution_status = "BLOCKED"
            entry.execution_reason = f"runtime_lookup:{e}"
            return entry

        if runtime is None:
            entry.execution_status = "BLOCKED"
            entry.execution_reason = "paper_runtime_not_attached"
            return entry

        try:
            from backend.paper.market_scan_loop import signal_to_paper_payload
            payload = signal_to_paper_payload(sig, expiry=entry.expiry or "")
            if not payload:
                entry.execution_status = "REJECTED"
                entry.execution_reason = "signal_payload_incomplete"
                entry.decision = (entry.decision or "") + " | REJECTED: payload incomplete"
                return entry
            result = runtime.submit_entry(payload)
            accepted = bool(getattr(result, "accepted", False))
            reason = getattr(result, "reason", "") or ""
            if accepted:
                entry.execution_status = "SUBMITTED"
                entry.execution_reason = reason or "submitted"
                entry.decision = (entry.decision or "") + " | EXECUTION: SUBMITTED"
            else:
                entry.execution_status = "REJECTED"
                entry.execution_reason = reason or "rejected"
                entry.decision = (entry.decision or "") + f" | REJECTED: {entry.execution_reason}"
        except Exception as e:
            entry.execution_status = "BLOCKED"
            entry.execution_reason = f"submit_error:{type(e).__name__}"
            entry.decision = (entry.decision or "") + f" | BLOCKED: {entry.execution_reason}"
        return entry

    def scan_once(self) -> List[ScannerEntry]:
        """One full pass over the currently configured universe. Synchronous
        — safe to call directly from tests or an API request."""
        symbols = self.universe_resolver()
        results = [self.scan_symbol(sym) for sym in symbols]
        self.last_full_pass_completed_at = datetime.now(timezone.utc).isoformat()
        self.currently_scanning = None
        return results

    # ── background loop ───────────────────────────────────────────────────

    async def run_forever(self) -> None:
        self._should_run = True
        self.is_running = True
        self._started_at_mono = time.monotonic()
        logger.info("Live scanner started")

        # Lifecycle event
        try:
            from backend.health.health_monitor import health_monitor, ComponentStatus
            health_monitor.update_status("scanner", ComponentStatus.RUNNING)
            health_monitor.log_event(
                "scanner", "SCANNER_STARTED", "Live scanner background task started",
            )
        except Exception:
            pass

        try:
            while self._should_run:
                pass_start = time.monotonic()
                try:
                    # Use asyncio.to_thread for the potentially-blocking
                    # universe_resolver call (event-loop audit fix).
                    symbols = await asyncio.to_thread(self.universe_resolver)
                    self._cached_watching_count = len(symbols) if symbols else 0
                    if not symbols:
                        await asyncio.sleep(self.seconds_between_symbols)
                        continue
                    for sym in symbols:
                        if not self._should_run:
                            break
                        try:
                            await asyncio.to_thread(self.scan_symbol, sym)
                        except Exception as e:
                            logger.warning("Scanner error on %s: %s", sym, e)
                        await asyncio.sleep(self.seconds_between_symbols)
                    self.last_full_pass_completed_at = datetime.now(timezone.utc).isoformat()
                    self.currently_scanning = None

                    # ── heartbeat update on successful pass ──────────────
                    self._last_scan_mono = time.monotonic()
                    self._last_scan_duration = self._last_scan_mono - pass_start
                    self._scan_count += 1
                    self._consecutive_scan_failures = 0
                    self._last_scan_error = None

                    # Report to health monitor
                    try:
                        from backend.health.health_monitor import health_monitor
                        health_monitor.heartbeat(
                            "scanner",
                            scan_count=self._scan_count,
                            last_scan_duration=round(self._last_scan_duration, 2),
                        )
                    except Exception:
                        pass

                except asyncio.CancelledError:
                    raise  # let CancelledError propagate — this is intentional stop
                except Exception as e:
                    # ── CRITICAL FIX: outer exception handler ────────────
                    # Without this, ANY exception in the universe_resolver
                    # or between per-symbol tries would kill the entire
                    # scanner task permanently.
                    self._consecutive_scan_failures += 1
                    self._last_scan_error = str(e)
                    logger.error(
                        "Scanner pass failed (consecutive failures: %d): %s",
                        self._consecutive_scan_failures, e,
                        exc_info=True,
                    )
                    try:
                        from backend.health.health_monitor import health_monitor, ComponentStatus
                        health_monitor.record_error("scanner", str(e))
                        health_monitor.log_event(
                            "scanner", "SCANNER_ERROR",
                            f"Scanner pass failed: {e} (consecutive: {self._consecutive_scan_failures})",
                            severity="ERROR",
                            exception=str(e),
                        )
                        if self._consecutive_scan_failures >= 5:
                            health_monitor.update_status("scanner", ComponentStatus.DEGRADED)
                    except Exception:
                        pass
                    # Wait before retrying — exponential backoff capped at 60s
                    backoff = min(60, self._consecutive_scan_failures * 5)
                    await asyncio.sleep(backoff)
        finally:
            self.is_running = False
            logger.info("Live scanner stopped")
            try:
                from backend.health.health_monitor import health_monitor, ComponentStatus
                health_monitor.update_status("scanner", ComponentStatus.STOPPED)
                health_monitor.log_event(
                    "scanner", "SCANNER_STOPPED", "Live scanner background task stopped",
                )
            except Exception:
                pass

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.ensure_future(self.run_forever())

    def stop(self) -> None:
        self._should_run = False
        if self._task is not None:
            self._task.cancel()

    # ── status for the dashboard ──────────────────────────────────────────

    def status_report(self) -> Dict[str, Any]:
        with self._results_lock:
            results_snapshot = list(self._results.values())
        watching_count = self._cached_watching_count
        if watching_count == 0:
            try:
                watching_count = len(self.universe_resolver())
                self._cached_watching_count = watching_count
            except Exception:
                watching_count = len(results_snapshot)
        return {
            "is_running": self.is_running,
            "currently_scanning": self.currently_scanning,
            "last_full_pass_completed_at": self.last_full_pass_completed_at,
            "watching_count": watching_count,
            "results": [e.to_dict() for e in results_snapshot],
        }

    def health_report(self) -> Dict[str, Any]:
        """Detailed health report with heartbeat data — proves liveness
        rather than just reading a boolean flag."""
        now = time.monotonic()
        seconds_since_last_scan = (
            round(now - self._last_scan_mono, 1) if self._last_scan_mono else None
        )
        is_healthy = (
            self.is_running
            and self._last_scan_mono > 0
            and (now - self._last_scan_mono) < SCANNER_HEALTH_TIMEOUT_SECONDS
        )
        # Determine effective status
        if not self.is_running:
            if self._should_run:
                scanner_status = "STARTING"
            else:
                scanner_status = "STOPPED"
        elif not is_healthy:
            scanner_status = "DEGRADED"
        elif self._consecutive_scan_failures > 0:
            scanner_status = "DEGRADED"
        else:
            scanner_status = "RUNNING"

        return {
            "scanner_status": scanner_status,
            "is_running": self.is_running,
            "is_healthy": is_healthy,
            "last_scan_seconds_ago": seconds_since_last_scan,
            "last_scan_duration_seconds": round(self._last_scan_duration, 2) if self._last_scan_duration else None,
            "scan_count": self._scan_count,
            "consecutive_failures": self._consecutive_scan_failures,
            "last_scan_error": self._last_scan_error,
            "currently_scanning": self.currently_scanning,
            "last_full_pass_completed_at": self.last_full_pass_completed_at,
            "uptime_seconds": round(now - self._started_at_mono, 1) if self._started_at_mono else 0,
        }

    def get_result(self, symbol: str) -> Optional[ScannerEntry]:
        with self._results_lock:
            return self._results.get(symbol)
