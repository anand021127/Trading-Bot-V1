#!/usr/bin/env python3
"""Long-running Paper worker.

Owns PaperTradingRuntime, heartbeats to SQLite, respects BotState start/stop/kill,
and is the only process allowed to submit paper entries through ExecutionPipeline.

Does not place live/real-money orders.
Does not modify V8-D strategy logic.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# Bound native BLAS thread pools BEFORE numpy/pandas are imported. Each
# OpenBLAS thread reserves large per-thread buffers; on small-RAM Windows
# hosts a spawned worker previously failed startup with "OpenBLAS error:
# Memory allocation still failed after 10 retries" under memory pressure.
# A single-strategy options scanner gains nothing from multithreaded BLAS.
# setdefault: an explicit operator config in the environment still wins.
for _blas_var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_blas_var, "1")

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.config.runtime_config import get_effective_settings
from backend.database.db_manager import DatabaseManager
from backend.paper.paper_runtime import PaperStartupError, PaperTradingRuntime
from backend.paper.scan_state import (
    MARKET_SCAN_KEY, SCAN_INFLIGHT_KEY, SCAN_SEQ_KEY,
    build_scan_record, describe_exception, persist_scan_record, scan_interval_seconds,
)
from backend.paper.worker_lock import WorkerLock, WorkerLockError
from backend.strategy.trading_engine import BotState

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [paper_worker] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("paper_worker")

HB_KEY = "paper_worker_heartbeat"
PID_KEY = "paper_worker_pid"
STATUS_KEY = "paper_worker_status"
ERR_KEY = "paper_worker_last_error"
LOOP_KEY = "paper_worker_loop_count"
PIPE_KEY = "paper_worker_pipeline_ok"


class PaperWorker:
    def __init__(self) -> None:
        self.db_path = os.environ.get("DATABASE_PATH", "data/trading_bot.db")
        self.lock_path = os.environ.get(
            "PAPER_WORKER_LOCK",
            str(Path(self.db_path).with_suffix("")) + ".paper_worker.lock",
        )
        self.db = DatabaseManager(db_path=self.db_path)
        BotState._db = self.db
        self.lock = WorkerLock(self.lock_path)
        self.runtime: Optional[PaperTradingRuntime] = None
        self._stop = False
        self._loop = 0
        # ── scan scheduling / diagnostics (see backend/paper/scan_state.py) ──
        self.scanner = None
        self._suppress_market_scan = False
        self._scan_interval = scan_interval_seconds()
        self._next_scan_mono = 0.0
        self._last_scanner_init_mono = 0.0
        try:
            self._scan_seq = int(self.db.get_setting(SCAN_SEQ_KEY, "0") or 0)
        except (TypeError, ValueError):
            self._scan_seq = 0
        # liveness pump shared state
        self._last_tick_mono = time.monotonic()
        self._scan_inflight_since: Optional[float] = None
        self._hb_stop = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None

    def _write_hb(self, status: str, error: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.db.save_setting(HB_KEY, now)
        self.db.save_setting(PID_KEY, str(os.getpid()))
        self.db.save_setting(STATUS_KEY, status)
        self.db.save_setting(LOOP_KEY, str(self._loop))
        if error:
            self.db.save_setting(ERR_KEY, error[:500])
        pipe_ok = "true" if self.runtime is not None and self.runtime.pipeline is not None else "false"
        self.db.save_setting(PIPE_KEY, pipe_ok)

    def _handle_signal(self, signum, _frame) -> None:
        logger.warning("Received signal %s — shutting down paper worker", signum)
        self._stop = True

    def start(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        try:
            self.lock.acquire()
        except WorkerLockError:
            # A SECOND worker that loses the lock race must change NOTHING in
            # the shared state: it used to write its own pid, status
            # "lock_failed" and the error text into the very rows the healthy
            # worker owns — then exit. Result: a stale "Paper worker already
            # running (pid=…)" error pinned on a perfectly healthy worker, and
            # a pid that briefly pointed at a dead process. The loser just
            # exits (main() logs it and returns code 2).
            raise

        try:
            self.runtime = PaperTradingRuntime(db=self.db)
        except PaperStartupError as exc:
            self._write_hb("startup_failed", str(exc))
            self.lock.release()
            raise

        self._init_market_scanner()
        try:
            # A previous run's error must not linger on a worker that just
            # started cleanly (it is re-set by the next real failure).
            self.db.save_setting(ERR_KEY, "")
        except Exception:
            pass
        self._write_hb("running")
        self._start_heartbeat_pump()
        logger.info(
            "Paper worker started pid=%s strategy=%s product=%s db=%s",
            os.getpid(),
            self.runtime.env["strategy"],
            self.runtime.env["product"],
            self.db_path,
        )
        self._loop_forever()

    def _should_run(self) -> bool:
        if self._stop:
            return False
        st = BotState.status()
        if st.get("kill_switch_active"):
            return False
        # Worker stays alive while process is up; trading activity only when BotState running
        return True

    def _trading_enabled(self) -> bool:
        st = BotState.status()
        return bool(st.get("running")) and not bool(st.get("kill_switch_active"))

    def _tick(self) -> None:
        assert self.runtime is not None
        self._loop += 1
        self._last_tick_mono = time.monotonic()
        if not self._trading_enabled():
            self._write_hb("idle_waiting_for_start")
            return

        # EOD check every loop when trading enabled
        try:
            self.runtime.run_eod()
        except Exception as exc:
            logger.exception("EOD error")
            self._write_hb("eod_error", str(exc))

        # Manual exits queued by the API process (cross-process queue in DB)
        try:
            self.runtime.drain_manual_exit_queue()
        except Exception as exc:
            logger.exception("Manual exit queue drain failed")
            self._write_hb("manual_exit_error", str(exc))

        # Reconcile periodically. PHASE 5.2 §5: the verdict is PERSISTED so
        # the AI decision layer reads the REAL reconciliation state (never a
        # hardcoded ok) — a failed reconcile also blocks new AI evaluation.
        if self._loop % 5 == 0:
            try:
                rec = self.runtime.reconcile()
                ok = bool(rec.get("ok"))
                self.db.save_setting("paper_reconcile_ok", "1" if ok else "0")
                self.db.save_setting(
                    "paper_reconcile_detail",
                    __import__("json").dumps({"ok": ok, "action": rec.get("action"),
                                              "error": rec.get("error"),
                                              "checked_at": __import__("datetime").datetime.now(
                                                  __import__("datetime").timezone.utc).isoformat()},
                                             default=str)[:1000],
                )
                if not ok:
                    logger.warning("Reconcile not ok: %s", rec)
            except Exception as exc:
                # Reconcile itself failed → state UNKNOWN: treat as not-ok
                # for AI evaluation purposes (fail closed, §5).
                try:
                    self.db.save_setting("paper_reconcile_ok", "0")
                    self.db.save_setting("paper_reconcile_detail",
                                         __import__("json").dumps({"ok": False, "error": type(exc).__name__,
                                                                   "checked_at": __import__("datetime").datetime.now(
                                                                       __import__("datetime").timezone.utc).isoformat()}))
                except Exception:
                    pass
                logger.exception("Reconcile error")
                self._write_hb("reconcile_error", str(exc))
                return

        # Evaluate open paper positions against latest option marks (real quotes only)
        try:
            self._evaluate_open_exits()
        except Exception:
            logger.exception("Paper exit evaluation failed")

        # Optional controlled test signal (never live). Enabled only via env for tests/demo.
        # MUST run BEFORE the market scan: the scan's instrument-master refresh
        # and candle backfill can block for tens of seconds per tick (cold
        # cache, upstox outage), which previously starved the test-signal
        # handler past the injector's wait window.
        if os.environ.get("PAPER_ALLOW_TEST_SIGNAL", "").strip() in {"1", "true", "yes"}:
            self._maybe_process_test_signal()

        # Market-driven V8-D scan (real Upstox data when scanner is armed).
        # EVERY due iteration persists a scan record — including market
        # closed, data errors, "scanner not armed" and exceptions — so the
        # dashboard/Copilot can prove the loop is executing.
        self._maybe_scan()

        self._write_hb("running")

    # ── scan iteration ──────────────────────────────────────────────────────
    def _persist(self, *, scanned: bool, traded: bool, reason: str, signal: Any = None,
                 details: Optional[Dict[str, Any]] = None, started: float,
                 error: Optional[str] = None) -> None:
        self._scan_seq += 1
        now = datetime.now(timezone.utc)
        rec = build_scan_record(
            seq=self._scan_seq, scanned=scanned, traded=traded, reason=reason,
            signal=signal, details=details,
            strategy=os.environ.get("TRADING_STRATEGY", "V8_D_PULLBACK_ATM"),
            underlying=(getattr(self.scanner, "underlying", None)
                        or os.environ.get("PAPER_UNDERLYING", "NIFTY50")),
            duration_ms=(time.monotonic() - started) * 1000.0,
            next_scan_at=now + timedelta(seconds=self._scan_interval),
            error=error, now=now,
        )
        persist_scan_record(self.db, rec)

    def _maybe_scan(self) -> None:
        assert self.runtime is not None
        if self._suppress_market_scan:
            return
        mono = time.monotonic()
        if mono < self._next_scan_mono:
            return
        self._next_scan_mono = mono + self._scan_interval
        started = mono

        # Scanner not armed (no token at startup / init failure): retry the
        # arming periodically — a token can appear AFTER the worker started
        # (OAuth completed later, daily token rotation) — and record the
        # state every iteration instead of staying silent forever.
        if self.scanner is None:
            if mono - self._last_scanner_init_mono >= 30.0:
                self._last_scanner_init_mono = mono
                self._init_market_scanner()
            if self.scanner is None:
                why = self.db.get_setting(MARKET_SCAN_KEY, "") or "disabled_no_token"
                self._persist(
                    scanned=False, traded=False, reason=f"scanner_disabled:{why}",
                    details={"data_status": "NO_MARKET_DATA_SOURCE",
                             "note": "No Upstox token / market-data source — V8-D cannot be evaluated"},
                    started=started,
                    error=f"market scanner not armed ({why})",
                )
                return

        try:
            st = BotState.status()
            # Authoritative, day-scoped counter (rolls at the IST/paper day
            # boundary). The previous persisted `paper_trades_today` setting
            # was a LIFETIME counter that never reset, so after
            # MAX_TRADES_PER_DAY cumulative trades every scan on every future
            # day would be rejected with "Daily trade limit reached".
            try:
                self.runtime._roll_day_if_needed()
            except Exception:
                pass
            trades_today = int(getattr(self.runtime, "trades_today", 0) or 0)
            self._scan_inflight_since = time.monotonic()
            self.db.save_setting(SCAN_INFLIGHT_KEY, datetime.now(timezone.utc).isoformat())
            try:
                scan = self.scanner.scan_once(
                    self.runtime,
                    trades_today=trades_today,
                    kill_switch_active=bool(st.get("kill_switch_active")),
                )
            finally:
                self._scan_inflight_since = None
                try:
                    self.db.save_setting(SCAN_INFLIGHT_KEY, "")
                except Exception:
                    pass
            self._persist(
                scanned=scan.scanned, traded=scan.traded, reason=scan.reason,
                signal=scan.signal, details=scan.details, started=started,
                error=(scan.details or {}).get("error"),
            )
            if not (scan.details or {}).get("error"):
                # An iteration completed without error → any earlier worker
                # error is resolved; stop showing it as a current problem.
                try:
                    if self.db.get_setting(ERR_KEY, ""):
                        self.db.save_setting(ERR_KEY, "")
                except Exception:
                    pass
            try:
                self.db.save_setting("paper_trades_today",
                                     str(int(getattr(self.runtime, "trades_today", 0) or 0)))
            except Exception:
                pass
            if scan.traded:
                logger.info("Paper trade taken via market scan: %s", scan.details)
        except Exception as exc:
            # NEVER swallow silently: log with traceback, persist a full scan
            # record carrying the (redacted) error, and expose it as
            # paper_worker_last_error so the UI shows RUNNING_SCANNER_ERROR.
            logger.exception("Market scan tick failed")
            msg = describe_exception(exc)
            self._persist(scanned=False, traded=False,
                          reason=f"scan_error:{type(exc).__name__}",
                          details={"error": msg}, started=started, error=msg)
            try:
                self.db.save_setting(ERR_KEY, msg[:500])
            except Exception:
                pass

    # ── liveness pump ───────────────────────────────────────────────────────
    def _start_heartbeat_pump(self) -> None:
        """Write the heartbeat from a separate thread (own SQLite handle).

        The main loop can legitimately block for tens of seconds inside a scan
        (instrument-master refresh / candle backfill / slow Upstox). Without
        this, the heartbeat went stale during long scans and the API reported
        the worker as dead. The pump refuses to write when the main loop has
        genuinely stopped making progress (no tick for 60s, or a scan in
        flight for >180s), so a hung loop is still reported as not responding.
        """
        def _pump() -> None:
            hb_db = None
            try:
                hb_db = DatabaseManager(db_path=self.db_path)
                while not self._hb_stop.wait(5.0):
                    now = time.monotonic()
                    inflight = self._scan_inflight_since
                    limit = 180.0 if inflight is not None else 60.0
                    if now - self._last_tick_mono > limit:
                        continue  # main loop stalled — let the heartbeat go stale
                    hb_db.save_setting(HB_KEY, datetime.now(timezone.utc).isoformat())
            except Exception:
                logger.exception("heartbeat pump stopped")
            finally:
                try:
                    if hb_db is not None:
                        hb_db.close()
                except Exception:
                    pass

        self._hb_thread = threading.Thread(target=_pump, name="paper-worker-heartbeat", daemon=True)
        self._hb_thread.start()

    def _maybe_process_test_signal(self) -> None:
        """If a pending test signal is stored in settings, submit once through the pipeline."""
        assert self.runtime is not None
        raw = self.db.get_setting("paper_test_signal_json", "")
        if not raw:
            return
        flag = self.db.get_setting("paper_test_signal_pending", "false")
        if flag != "true":
            return
        try:
            payload = json.loads(raw)
        except Exception as exc:
            self.db.save_setting("paper_test_signal_pending", "false")
            self._write_hb("test_signal_bad_json", str(exc))
            return
        # Clear pending first to avoid duplicate on crash mid-submit
        self.db.save_setting("paper_test_signal_pending", "false")
        # Deterministic test/demo runs: the market scan's instrument-master
        # refresh + candle backfill can block ticks for tens of seconds (cold
        # cache, upstox outage), which delays test-signal result visibility
        # far past the injector's wait window. After a test signal arrives,
        # park the scanner so subsequent ticks service it promptly. Real
        # market-driven trading is never suppressed when no test signal is
        # queued, and the suppression is per-worker-process (never persisted).
        self._suppress_market_scan = True
        try:
            result = self.runtime.submit_entry(payload)
            self.db.save_setting(
                "paper_test_signal_result",
                json.dumps(
                    {
                        "accepted": getattr(result, "accepted", None),
                        "reason": getattr(result, "reason", None),
                        "signal_id": getattr(result, "signal_id", None),
                        "ts": datetime.now(timezone.utc).isoformat(),
                    },
                    default=str,
                ),
            )
            logger.info(
                "Test signal processed accepted=%s reason=%s",
                getattr(result, "accepted", None),
                getattr(result, "reason", None),
            )
        except Exception as exc:
            self.db.save_setting("paper_test_signal_result", json.dumps({"error": str(exc)}))
            logger.exception("Test signal failed")



    def _evaluate_open_exits(self) -> None:
        """Push latest option LTP into the paper exit engine for each open position.

        Production bug fixed: this used to call client.get_ltp()/
        get_market_quote_ltp(), which do not exist on UpstoxClient — so the
        quote lookup silently failed every tick and open paper positions
        NEVER exited on stop-loss / target / trailing stop during the day
        (they only closed via EOD square-off). Uses the real
        get_quote_by_instrument_key API now; skips ticks that are missing,
        unparsable, or non-positive rather than inventing a mark.
        """
        assert self.runtime is not None
        positions = list(self.runtime.broker.positions.items())
        if not positions:
            return
        client = getattr(self.scanner, "data", None)
        client = getattr(client, "client", None) if client is not None else None
        for ik, pos in positions:
            if int(pos.get("quantity") or 0) <= 0:
                continue
            mark = None
            if client is not None and hasattr(client, "get_quote_by_instrument_key"):
                try:
                    q = client.get_quote_by_instrument_key(ik)
                    if isinstance(q, dict):
                        candidate = q.get("ltp")
                        if candidate is not None:
                            mark = float(candidate)
                except Exception:
                    mark = None
            if mark is None:
                continue
            if not (mark > 0):
                # ltp=0/None placeholders must never become an exit mark
                continue
            self.runtime.on_option_quote(ik, mark)

    @staticmethod
    def _current_account_equity(db: Any) -> float:
        """PHASE 5.3 §7: CURRENT equity for risk decisions, never a frozen
        startup constant.

        Priority: 1) the persisted equity snapshot the runtime maintains
        (realized P&L-adjusted, survives restarts), 2) the AUTHORITATIVE
        configured capital (Settings-DB blob over env — phase-B fix; this
        used to read TRADING_CAPITAL env directly, so a user who saved
        capital=₹20,000 in the Settings UI still seeded scans at ₹100,000).
        The scanner additionally re-reads runtime.realized_equity on every
        scan (scan_once); this value only seeds it before the first tick.
        """
        try:
            raw = db.get_setting("paper_equity_snapshot", "") or ""
            if raw:
                snap = json.loads(raw)
                eq = float(snap.get("realized_equity") or 0)
                if eq > 0:
                    return eq
        except Exception:
            pass
        try:
            return float(get_effective_settings().capital.total)
        except (TypeError, ValueError):
            return 100000.0

    def _init_market_scanner(self) -> None:

        """Attach Upstox-backed scanner when a token is available; else leave offline."""
        from backend.paper.market_scan_loop import PaperMarketScanner, UpstoxMarketDataSource
        # Prefer the AUTHORITATIVE resolver (re-read on every request, so a
        # daily-rotated token is picked up without restarting the worker).
        # Pinning an explicit token into the client froze the FIRST token for
        # the life of the process → every call 401'd after the next rotation.
        resolver_token = ""
        try:
            from backend.broker.token_resolver import resolve_upstox_token
            resolver_token = (resolve_upstox_token() or "").strip()
        except Exception:
            resolver_token = ""
        token = resolver_token or os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
        if not token:
            try:
                token = self.db.load_token(require_valid=False) or ""
            except Exception:
                token = ""
        if not token:
            logger.info("No Upstox token — market-driven scan disabled (paper runtime still armed)")
            self.db.save_setting("paper_worker_market_scan", "disabled_no_token")
            return
        try:
            from backend.broker.upstox_client import UpstoxClient
            client = UpstoxClient() if resolver_token else UpstoxClient(access_token=token)
            data = UpstoxMarketDataSource(client)
            # PHASE 5.1: AI trading decision engine — shares the worker's DB
            # (durable ai_decisions table + latency telemetry). Disabled by
            # default (AI_DECISION_ENABLED=false → V8-D-only scan); when
            # enabled it gates every V8-D BUY before the hard risk gates.
            ai_engine = None
            ai_note = "disabled"
            try:
                from backend.ai_decision.decision_engine import AITradingDecisionEngine
                ai_engine = AITradingDecisionEngine(db=self.db)
                ai_note = "enabled" if ai_engine.enabled else "disabled"
            except Exception as exc:
                logger.warning("AI decision engine init failed: %s", type(exc).__name__)
                ai_note = f"init_failed:{type(exc).__name__}"
                ai_engine = None
            self.db.save_setting("ai_decision_layer", ai_note)
            self.scanner = PaperMarketScanner(
                data=data,
                strategy=self.runtime.strategy,
                underlying=os.environ.get("PAPER_UNDERLYING", "NIFTY50"),
                account_equity=self._current_account_equity(self.db),
                max_candle_age_seconds=float(os.environ.get("PAPER_MAX_CANDLE_AGE_SEC", "900")),
                min_bars=int(os.environ.get("PAPER_MIN_CANDLE_BARS", "60")),
                ai_engine=ai_engine,
                ai_decision_pipeline_strategy=os.environ.get("TRADING_STRATEGY", "V8_D_PULLBACK_ATM"),
            )
            self.db.save_setting("paper_worker_market_scan", "enabled")
            logger.info(
                "Market-driven V8-D scanner enabled for %s (AI decision layer: %s)",
                self.scanner.underlying, ai_note,
            )
        except Exception as exc:
            logger.warning("Could not init market scanner: %s", type(exc).__name__)
            self.db.save_setting("paper_worker_market_scan", f"init_failed:{type(exc).__name__}")
            self.scanner = None

    def _loop_forever(self) -> None:
        interval = float(os.environ.get("PAPER_WORKER_INTERVAL_SEC", "2"))
        try:
            while self._should_run():
                try:
                    self._tick()
                except Exception as exc:
                    logger.exception("Worker tick failed")
                    self._write_hb("tick_error", str(exc))
                time.sleep(max(0.5, interval))
        finally:
            self._hb_stop.set()
            self._write_hb("stopped")
            self.lock.release()
            try:
                self.db.close()
            except Exception:
                pass
            logger.info("Paper worker stopped pid=%s", os.getpid())


def main() -> int:
    try:
        PaperWorker().start()
        return 0
    except WorkerLockError as exc:
        logger.error("%s", exc)
        return 2
    except PaperStartupError as exc:
        logger.error("Startup failed: %s", exc)
        return 3
    except Exception:
        logger.error("Fatal: %s", traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
