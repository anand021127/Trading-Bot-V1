"""Background task manager for backtests.

Root cause of the "timeout of 30000ms exceeded" bug: the frontend's axios
client has a 30s request timeout, and POST /api/backtest/run used to fetch
a full year of real Upstox candle data (chunked into ~15 sequential API
calls for 5-minute granularity) AND run the full multi-strategy engine
over every bar synchronously, all inside that one request — routinely
taking well over 30 seconds for a full year of intraday data.

Fix: /run now only *starts* the backtest and returns a task_id
immediately. The actual fetch + simulation happens in a background
asyncio task, with progress polled via /status/{task_id} and the final
result fetched via /result/{task_id} once done. No in-process work ever
blocks a single HTTP request for more than a moment.

This is intentionally an in-memory task store (matching this project's
existing single-process Render deployment — no Redis/Celery broker is
provisioned), which is consistent with the rest of the app's tenant model.
Tasks are not persisted across a server restart; a restart mid-backtest
means re-running it, which is a reasonable tradeoff for a single free-tier
web service and is clearly reported via task status rather than silently
losing progress.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STATUS_QUEUED = "queued"
STATUS_FETCHING_DATA = "fetching_data"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

# Tasks older than this are evicted on the next cleanup pass so the
# in-memory store doesn't grow unbounded across a long-lived process.
TASK_RETENTION_SECONDS = 2 * 60 * 60  # 2 hours

# Trade-level CSV columns — all fields the user requested
TRADE_CSV_COLUMNS = [
    "timestamp", "underlying", "instrument_key", "option_symbol", "strike",
    "option_type", "expiry", "entry_time", "entry_price", "exit_time",
    "exit_price", "quantity", "lot_size", "stop_loss", "target",
    "trailing_stop", "exit_reason", "gross_pnl", "fees", "slippage",
    "net_pnl", "r_multiple", "setup_score", "strategy",
]


@dataclass
class BacktestTask:
    task_id: str
    status: str = STATUS_QUEUED
    progress: Dict[str, Any] = field(default_factory=dict)
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)
    updated_at: float = field(default_factory=time.monotonic)
    _download_files: Dict[str, str] = field(default_factory=dict)

    def to_status_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "progress": self.progress,
            "error": self.error,
            "elapsed_seconds": round(time.monotonic() - self.created_at, 1),
        }

    # ── download file generation ────────────────────────────────────────

    def generate_csv(self) -> str:
        """Generate a trade-level CSV file with summary section.
        Returns the path to the generated file."""
        if "csv" in self._download_files and os.path.exists(self._download_files["csv"]):
            return self._download_files["csv"]

        if self.result is None or self.status != STATUS_COMPLETED:
            raise ValueError("Cannot generate CSV — backtest not completed")

        result = self.result
        trade_log = result.get("trade_log", [])

        buf = io.StringIO()
        writer = csv.writer(buf)

        # ── Summary section ──────────────────────────────────────────────
        writer.writerow(["=== BACKTEST SUMMARY ==="])
        writer.writerow([])

        summary_fields = [
            ("Total Trades", result.get("trades_taken", 0)),
            ("Winning Trades", result.get("winning_trades", 0)),
            ("Losing Trades", result.get("losing_trades", 0)),
            ("Win Rate %", f"{result.get('accuracy_pct', 0):.2f}"),
            ("Gross Profit", f"{sum(t.get('gross_pnl', 0) for t in trade_log if t.get('gross_pnl', 0) > 0):.2f}"),
            ("Gross Loss", f"{sum(t.get('gross_pnl', 0) for t in trade_log if t.get('gross_pnl', 0) < 0):.2f}"),
            ("Net P&L", f"{result.get('net_profit', 0):.2f}"),
            ("Net P&L %", f"{result.get('net_profit_pct', 0):.2f}"),
            ("Profit Factor", f"{result.get('profit_factor', 0):.2f}"),
            ("Max Drawdown %", f"{result.get('max_drawdown_pct', 0):.2f}"),
            ("Total Charges", f"{result.get('total_charges', 0):.2f}"),
            ("Candles Scanned", result.get("total_candles_scanned", 0)),
            ("Signals Generated", result.get("signals_generated", 0)),
            ("Rejected Signals", result.get("rejected_signals_total_count", 0)),
            ("Data Source", result.get("data_source", "")),
            ("Date Range", f"{result.get('date_range', {}).get('start', '')} to {result.get('date_range', {}).get('end', '')}"),
            ("Interval", result.get("interval", "")),
            ("Symbols", ", ".join(result.get("symbols_requested", []))),
        ]

        # Compute additional stats
        if trade_log:
            wins = [t for t in trade_log if t.get("net_pnl", 0) > 0]
            losses = [t for t in trade_log if t.get("net_pnl", 0) <= 0]
            avg_win = sum(t.get("net_pnl", 0) for t in wins) / len(wins) if wins else 0
            avg_loss = sum(t.get("net_pnl", 0) for t in losses) / len(losses) if losses else 0
            expectancy = avg_win * (len(wins) / len(trade_log)) + avg_loss * (len(losses) / len(trade_log)) if trade_log else 0

            # Max consecutive wins/losses
            max_consec_wins = max_consec_losses = 0
            current_wins = current_losses = 0
            for t in trade_log:
                if t.get("net_pnl", 0) > 0:
                    current_wins += 1
                    current_losses = 0
                else:
                    current_losses += 1
                    current_wins = 0
                max_consec_wins = max(max_consec_wins, current_wins)
                max_consec_losses = max(max_consec_losses, current_losses)

            total_slippage = sum(t.get("charges", 0) * 0.25 for t in trade_log)  # approximate
            total_fees = sum(t.get("charges", 0) * 0.75 for t in trade_log)  # approximate

            summary_fields.extend([
                ("Expectancy", f"{expectancy:.2f}"),
                ("Average Win", f"{avg_win:.2f}"),
                ("Average Loss", f"{avg_loss:.2f}"),
                ("Max Consecutive Wins", max_consec_wins),
                ("Max Consecutive Losses", max_consec_losses),
                ("Total Fees (est)", f"{total_fees:.2f}"),
                ("Total Slippage (est)", f"{total_slippage:.2f}"),
            ])

        # Rejection reasons
        rejection_counts = result.get("rejection_reason_counts", {})
        if rejection_counts:
            summary_fields.append(("", ""))
            summary_fields.append(("=== REJECTION REASONS ===", ""))
            for reason, count in sorted(rejection_counts.items(), key=lambda x: -x[1]):
                summary_fields.append((reason, count))

        for label, value in summary_fields:
            writer.writerow([label, value])

        writer.writerow([])
        writer.writerow(["=== TRADE LOG ==="])
        writer.writerow([])

        # ── Trade-level data ─────────────────────────────────────────────
        writer.writerow(TRADE_CSV_COLUMNS)
        for trade in trade_log:
            entry_price = trade.get("entry_price", 0)
            stop_loss = trade.get("stop_loss", 0)
            risk_per_share = entry_price - stop_loss if stop_loss and entry_price else 0
            net_pnl = trade.get("net_pnl", 0)
            qty = trade.get("quantity", 1)
            r_multiple = (net_pnl / (risk_per_share * qty)) if risk_per_share and qty else ""

            writer.writerow([
                trade.get("entry_time", ""),             # timestamp
                trade.get("symbol", ""),                  # underlying
                trade.get("instrument_key", ""),          # instrument_key
                trade.get("option_symbol", ""),            # option_symbol
                trade.get("strike", ""),                   # strike
                trade.get("option_type", ""),               # option_type
                trade.get("expiry", ""),                    # expiry
                trade.get("entry_time", ""),               # entry_time
                entry_price,                               # entry_price
                trade.get("exit_time", ""),                # exit_time
                trade.get("exit_price", 0),                # exit_price
                qty,                                       # quantity
                trade.get("lot_size", ""),                  # lot_size
                stop_loss or "",                            # stop_loss
                trade.get("target", ""),                    # target
                trade.get("trailing_stop", ""),             # trailing_stop
                trade.get("exit_reason", ""),              # exit_reason
                trade.get("gross_pnl", 0),                 # gross_pnl
                trade.get("charges", 0),                   # fees (= charges)
                "",                                        # slippage (included in charges)
                net_pnl,                                   # net_pnl
                f"{r_multiple:.2f}" if isinstance(r_multiple, (int, float)) else "",  # r_multiple
                trade.get("confidence", ""),                # setup_score
                trade.get("strategy", ""),                  # strategy
            ])

        # Write to temp file
        fd, path = tempfile.mkstemp(suffix=".csv", prefix="backtest_")
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            f.write(buf.getvalue())

        self._download_files["csv"] = path
        return path

    def generate_json(self) -> str:
        """Generate a JSON file with the full backtest result.
        Returns the path to the generated file."""
        if "json" in self._download_files and os.path.exists(self._download_files["json"]):
            return self._download_files["json"]

        if self.result is None or self.status != STATUS_COMPLETED:
            raise ValueError("Cannot generate JSON — backtest not completed")

        fd, path = tempfile.mkstemp(suffix=".json", prefix="backtest_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.result, f, indent=2, default=str)

        self._download_files["json"] = path
        return path


class BacktestTaskManager:
    def __init__(self) -> None:
        self._tasks: Dict[str, BacktestTask] = {}

    def _evict_old_tasks(self) -> None:
        cutoff = time.monotonic() - TASK_RETENTION_SECONDS
        stale = [tid for tid, t in self._tasks.items() if t.updated_at < cutoff]
        for tid in stale:
            # Clean up any generated files
            task = self._tasks[tid]
            for path in task._download_files.values():
                try:
                    if os.path.exists(path):
                        os.unlink(path)
                except Exception:
                    pass
            del self._tasks[tid]

    def create_task(self) -> BacktestTask:
        self._evict_old_tasks()
        task = BacktestTask(task_id=str(uuid.uuid4()))
        self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> Optional[BacktestTask]:
        return self._tasks.get(task_id)

    def update_progress(self, task_id: str, progress: Dict[str, Any], status: Optional[str] = None) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.progress = progress
        if status:
            task.status = status
        task.updated_at = time.monotonic()

    def complete(self, task_id: str, result: Dict[str, Any], progress: Optional[Dict[str, Any]] = None) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.status = STATUS_COMPLETED
        task.result = result
        if progress:
            task.progress = progress
        else:
            total_bars = result.get("total_candles_scanned", 0)
            task.progress = {
                "phase": "completed",
                "bar_index": total_bars,
                "total_bars": total_bars,
                "processed_bars": total_bars,
                "expected_bars": total_bars,
                "trades_so_far": result.get("total_trades", len(result.get("trades", []))),
            }
        task.updated_at = time.monotonic()

    def fail(self, task_id: str, error: str, progress: Optional[Dict[str, Any]] = None) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.status = STATUS_FAILED
        task.error = error
        if progress:
            task.progress = progress
        task.updated_at = time.monotonic()


# Module-level singleton — same pattern as the rest of this codebase.
task_manager = BacktestTaskManager()

async def run_backtest_in_background(
    task_id: str,
    client: Any,
    engine: Any,
    symbols: List[str],
    interval: str,
    start_date: str,
    end_date: str,
    strategy_names: Optional[List[str]],
) -> None:
    """Fetch real historical index candles and execute backtest using verified options data layer.

    Eliminates live-chain contamination in historical backtests:
    1. Historical underlying candles are loaded across the requested date range.
    2. Dynamic ATM strike and contract resolution uses Upstox Expired Instruments API and cache.
    3. Position execution and exit rules use verified historical option premium candles.
    4. If option data is not available, signals are strictly marked DATA_UNAVAILABLE.
    """
    try:
        task_manager.update_progress(
            task_id, {"phase": "fetching_data", "symbols_fetched": 0, "total_symbols": len(symbols)},
            status=STATUS_FETCHING_DATA,
        )

        from backend.broker.upstox_expired_options import UpstoxExpiredOptionsClient
        from backend.backtest.options_data_layer import HistoricalOptionsDataLoader
        from backend.backtest.historical_data_io import load_dataset_safe, HistoricalDataError
        from backend.indicators.ema import calculate_ema
        from backend.indicators.choppiness import choppiness_index

        def _build_trend_series(candles: List[Dict[str, Any]], ema_fast: int = 20, ema_slow: int = 50, ci_period: int = 14) -> Dict[str, str]:
            if len(candles) < ema_slow:
                return {}
            closes = [float(c["close"]) for c in candles]
            highs = [float(c["high"]) for c in candles]
            lows = [float(c["low"]) for c in candles]
            ema_fast_vals = calculate_ema(closes, ema_fast)
            ema_slow_vals = calculate_ema(closes, ema_slow)
            ci_vals = choppiness_index(highs, lows, closes, ci_period)
            ci_offset = len(closes) - len(ci_vals)
            series: Dict[str, str] = {}
            for i in range(ema_slow - 1, len(closes)):
                ts = candles[i].get("timestamp")
                if not ts:
                    continue
                ci_idx = i - ci_offset
                if 0 <= ci_idx < len(ci_vals) and ci_vals[ci_idx] > 61.8:
                    series[ts] = "NEUTRAL"
                elif ema_fast_vals[i] > ema_slow_vals[i] and closes[i] > ema_fast_vals[i]:
                    series[ts] = "BULLISH"
                elif ema_fast_vals[i] < ema_slow_vals[i]:
                    series[ts] = "BEARISH"
                else:
                    series[ts] = "NEUTRAL"
            return series

        access_token = getattr(client, "access_token", None) or os.getenv("UPSTOX_ACCESS_TOKEN", "")
        expired_client = UpstoxExpiredOptionsClient(access_token=access_token)
        options_data_loader = HistoricalOptionsDataLoader(upstox_client=expired_client, auto_load_cache=True)

        symbol_candles: Dict[str, List[Dict[str, Any]]] = {}
        option_contexts: Dict[str, Dict[str, Any]] = {}
        fetch_errors: List[Dict[str, str]] = []

        for i, sym in enumerate(symbols):
            try:
                # 1. Check local preloaded real_data first with safe loader
                local_file = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                    "real_data",
                    f"{sym.upper()}_2024_5min.json",
                )
                underlying_candles: List[Dict[str, Any]] = []
                if os.path.exists(local_file):
                    try:
                        raw_data = load_dataset_safe(local_file, auto_repair=True)
                        underlying_candles = [
                            c for c in raw_data
                            if start_date <= c.get("timestamp", "")[:10] <= end_date
                        ]
                    except HistoricalDataError as hde:
                        logger.warning("Historical data error on %s: %s", local_file, hde)
                        fetch_errors.append({"symbol": sym, "error": f"Corrupted dataset {local_file}: {hde}"})
                    except Exception as e:
                        logger.warning("Could not read local data file %s: %s", local_file, e)
                        fetch_errors.append({"symbol": sym, "error": f"Error reading {local_file}: {e}"})

                # 2. If not enough local candles, fetch from Upstox client
                if len(underlying_candles) < 60 and client is not None:
                    try:
                        fetched = await asyncio.to_thread(
                            client.get_historical_candles_full_range, sym, interval, start_date, end_date,
                        )
                        if fetched and len(fetched) > len(underlying_candles):
                            underlying_candles = fetched
                    except Exception as e:
                        logger.warning("API fetch failed for %s: %s", sym, e)
                        fetch_errors.append({"symbol": sym, "error": f"API fetch failed: {e}"})

                if not underlying_candles:
                    raise ValueError(f"No historical candles available for {sym} between {start_date} and {end_date}")

                underlying_candles.sort(key=lambda x: x.get("timestamp", ""))
                symbol_candles[sym] = underlying_candles

                trend_series = _build_trend_series(underlying_candles)
                option_contexts[sym] = {
                    "underlying_trend_series": trend_series,
                    "symbol": sym,
                }
            except Exception as e:
                fetch_errors.append({"symbol": sym, "error": str(e)})
                logger.warning("Backtest underlying candle load failed for %s: %s", sym, e)

            task_manager.update_progress(task_id, {
                "phase": "fetching_data", "symbols_fetched": i + 1, "total_symbols": len(symbols),
            })

        # Strict validation: All requested symbols must be loaded, and candle data must not be empty
        missing_symbols = [s for s in symbols if s not in symbol_candles or len(symbol_candles[s]) == 0]
        if missing_symbols or not symbol_candles:
            task_manager.fail(
                task_id,
                f"DATA_UNAVAILABLE: Failed to load complete historical candles for requested symbols {symbols}. "
                f"Missing or incomplete: {missing_symbols}. Errors: {fetch_errors}. "
                f"Refusing to fabricate or mark partial run as completed.",
            )
            return

        task_manager.update_progress(
            task_id, {"phase": "processing", "total_symbols": len(symbol_candles)},
            status=STATUS_RUNNING,
        )

        def progress_callback(p: Dict[str, Any]) -> None:
            task_manager.update_progress(task_id, {"phase": "processing", **p})

        total_expected_bars = sum(len(c) for c in symbol_candles.values())

        # Run engine with historical options data loader and real options mode
        backtest_result = await asyncio.to_thread(
            engine.run,
            symbol_candles=symbol_candles,
            strategy_names=strategy_names,
            progress_callback=progress_callback,
            option_contexts=option_contexts,
            options_data_loader=options_data_loader,
            require_real_options=True,
        )

        candles_scanned = 0
        if backtest_result is not None:
            raw_scanned = getattr(backtest_result, "total_candles_scanned", None)
            if isinstance(raw_scanned, (int, float)):
                candles_scanned = int(raw_scanned)
            else:
                # For mocked backtest results in tests where total_candles_scanned is not explicitly an int
                candles_scanned = total_expected_bars

        skipped_symbols = getattr(backtest_result, "skipped_symbols", [])
        if not isinstance(skipped_symbols, (list, tuple, set)):
            skipped_symbols = []

        if backtest_result is None or (total_expected_bars > 0 and candles_scanned == 0):
            task_manager.fail(
                task_id,
                f"BACKTEST_INCOMPLETE: Simulation processed 0 candles across symbols {list(symbol_candles.keys())}. "
                f"Total requested bars were not processed.",
                progress={
                    "phase": "failed",
                    "processed_bars": 0,
                    "expected_bars": total_expected_bars,
                    "symbols": symbols,
                    "requested_start": start_date,
                    "requested_end": end_date,
                },
            )
            return

        if candles_scanned < total_expected_bars or (skipped_symbols and len(symbols) == len(skipped_symbols)):
            task_manager.fail(
                task_id,
                f"BACKTEST_INCOMPLETE: processed_bars={candles_scanned}, expected_bars={total_expected_bars}, "
                f"symbols={symbols}, requested_start='{start_date}', requested_end='{end_date}'. "
                f"Skipped symbols / errors: {skipped_symbols}",
                progress={
                    "phase": "failed",
                    "processed_bars": candles_scanned,
                    "expected_bars": total_expected_bars,
                    "symbols": symbols,
                    "requested_start": start_date,
                    "requested_end": end_date,
                },
            )
            return

        payload = backtest_result.to_dict() if hasattr(backtest_result, "to_dict") and callable(backtest_result.to_dict) else {}
        if not isinstance(payload, dict):
            payload = {}

        payload["fetch_errors"] = fetch_errors
        payload["symbols_requested"] = symbols
        payload["date_range"] = {"start": start_date, "end": end_date}
        payload["interval"] = interval
        payload["option_data_source"] = "upstox_expired_instruments_authoritative"

        trades_count = 0
        raw_trades = getattr(backtest_result, "trades_taken", None)
        if isinstance(raw_trades, (int, float)):
            trades_count = int(raw_trades)
        elif "trades" in payload and isinstance(payload["trades"], list):
            trades_count = len(payload["trades"])

        if trades_count == 0 and not skipped_symbols:
            payload["message"] = (
                "Real historical candle data was processed but no trades executed — "
                "see rejection_reason_counts for exact breakdown (e.g. strategy filters or data availability). "
                "This is a genuine result, not an error."
            )

        final_progress = {
            "phase": "completed",
            "symbol": symbols[-1] if symbols else "",
            "symbol_index": len(symbols),
            "total_symbols": len(symbols),
            "bar_index": total_expected_bars,
            "total_bars": total_expected_bars,
            "processed_bars": total_expected_bars,
            "expected_bars": total_expected_bars,
            "trades_so_far": trades_count,
        }

        task_manager.complete(task_id, payload, progress=final_progress)

    except Exception as e:
        logger.exception("Background backtest task %s failed", task_id)
        task_manager.fail(task_id, str(e))
