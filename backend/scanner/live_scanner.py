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
    ) -> None:
        self.trading_engine = trading_engine
        self.universe_resolver = universe_resolver  # callable -> List[str]
        self.mode_resolver = mode_resolver or (lambda: "OPTIONS")
        self.seconds_between_symbols = seconds_between_symbols

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
            best = self.trading_engine.evaluate_option_premium(symbol)
            signals = [best]
        except Exception as e:
            entry.error = str(e)
            entry.decision = f"ERROR — {e}"
            with self._results_lock:
                self._results[symbol] = entry
            return entry

        entry.strategy_breakdown = [s.to_dict() for s in signals]

        ema_signal = next((s for s in signals if s.strategy_name == "EMA_TREND"), None)
        if ema_signal is not None:
            conds = ema_signal.conditions
            entry.ema_status = _status(
                conds.get("ema_trend_up") and conds.get("price_above_ema20")
                if conds else None
            )
            entry.rsi_value = ema_signal.indicators.get("rsi")
            entry.rsi_status = _status(conds.get("rsi_in_range")) if conds else "N/A"
            entry.atr = ema_signal.indicators.get("atr")
            entry.volume_status = _status(conds.get("volume_confirmed")) if conds else "N/A"
            entry.trend = "BULLISH" if conds.get("ema_trend_up") else "BEARISH" if conds else "NEUTRAL"
            if not entry.ltp:
                entry.ltp = ema_signal.entry_price or entry.ltp

        option_signal = next((s for s in signals if s.strategy_name == "OPTION_PREMIUM"), None)
        if option_signal is not None and not entry.ltp:
            entry.ltp = option_signal.entry_price or None
            contract = option_signal.indicators.get("selected_contract") if option_signal.indicators else None
            if contract:
                entry.trend = "BULLISH" if contract.get("option_type") == "CE" else "BEARISH"

        best = max(signals, key=lambda s: s.confidence, default=None)
        if best is not None:
            entry.decision = best.entry_reason
            entry.signal = best.signal
            entry.confidence = best.confidence
            entry.rejected_reasons = [
                reason for s in signals for reason in s.rejected_reasons
            ]

        with self._results_lock:
            self._results[symbol] = entry
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
