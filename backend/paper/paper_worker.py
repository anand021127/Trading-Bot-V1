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
import time
import traceback
from datetime import datetime, timezone
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

from backend.database.db_manager import DatabaseManager
from backend.paper.paper_runtime import PaperStartupError, PaperTradingRuntime
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
        except WorkerLockError as exc:
            self._write_hb("lock_failed", str(exc))
            raise

        try:
            self.runtime = PaperTradingRuntime(db=self.db)
        except PaperStartupError as exc:
            self._write_hb("startup_failed", str(exc))
            self.lock.release()
            raise

        self.scanner = None
        self._suppress_market_scan = False
        self._init_market_scanner()
        self._write_hb("running")
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

        # Reconcile periodically
        if self._loop % 5 == 0:
            try:
                rec = self.runtime.reconcile()
                if not rec.get("ok"):
                    logger.warning("Reconcile not ok: %s", rec)
            except Exception as exc:
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

        # Market-driven V8-D scan (real Upstox data when scanner is armed)
        if self.scanner is not None and not self._suppress_market_scan:
            try:
                from backend.strategy.trading_engine import BotState
                st = BotState.status()
                scan = self.scanner.scan_once(
                    self.runtime,
                    trades_today=int(self.db.get_setting("paper_trades_today", "0") or 0),
                    kill_switch_active=bool(st.get("kill_switch_active")),
                )
                self.db.save_setting("paper_worker_last_scan", scan.reason)
                self.db.save_setting(
                    "paper_worker_last_scan_detail",
                    __import__("json").dumps({
                        "scanned": scan.scanned,
                        "traded": scan.traded,
                        "reason": scan.reason,
                        "signal": scan.signal,
                        "details": scan.details,
                    }, default=str)[:2000],
                )
                if scan.traded:
                    n = int(self.db.get_setting("paper_trades_today", "0") or 0) + 1
                    self.db.save_setting("paper_trades_today", str(n))
                    logger.info("Paper trade taken via market scan: %s", scan.details)
            except Exception as exc:
                logger.exception("Market scan tick failed")
                self.db.save_setting("paper_worker_last_scan", f"scan_error:{type(exc).__name__}")

        self._write_hb("running")

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

    def _init_market_scanner(self) -> None:

        """Attach Upstox-backed scanner when a token is available; else leave offline."""
        from backend.paper.market_scan_loop import PaperMarketScanner, UpstoxMarketDataSource
        token = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
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
            client = UpstoxClient(access_token=token)
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
                account_equity=float(os.environ.get("TRADING_CAPITAL", "100000")),
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
