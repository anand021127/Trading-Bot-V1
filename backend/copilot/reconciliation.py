"""PHASE 8 reconciliation: TradePlan created at T -> observe real
subsequent premium candles -> determine target hit / SL hit / timeout ->
write the outcome back into the shadow log.

Reads backend/copilot/shadow_logger.py's CSV, resolves any row whose
`hypothetical_outcome` is still blank and old enough to have resolved,
using the REAL instrument's subsequent candles via the same broker
client (`engine.client.get_historical_candles`) — never simulated or
guessed. Rows that can't be resolved yet (too recent) or can't be
resolved at all (no instrument_key logged, or the broker has no data for
that period) are left/marked accordingly rather than guessed.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.copilot.shadow_logger import DEFAULT_LOG_PATH, FIELDS


def _resolve_outcome(
    client: Any,
    instrument_key: str,
    decision_timestamp: str,
    entry: float,
    stop_loss: float,
    target: float,
    horizon_bars: int = 48,
) -> str:
    """Returns 'TARGET_HIT', 'SL_HIT', 'TIMEOUT', or 'UNRESOLVED' (can't
    determine yet — e.g. not enough time/candles have passed, or the
    broker has no data for that instrument/period). Same-bar target+SL
    touches are resolved conservatively as SL_HIT — OHLC candles can't
    tell us the true intrabar order, so we never assume the favorable
    outcome when it's ambiguous (same convention as backend/ai/dataset.py's
    offline label builder)."""
    try:
        candles = client.get_historical_candles(instrument_key, "5minute", limit=horizon_bars + 20)
    except Exception:
        return "UNRESOLVED"
    if not candles:
        return "UNRESOLVED"

    forward = [c for c in candles if str(c.get("timestamp", "")) > decision_timestamp]
    if len(forward) < 1:
        return "UNRESOLVED"  # not enough time has passed yet, or data doesn't cover it

    for bar in forward[:horizon_bars]:
        hi, lo = float(bar["high"]), float(bar["low"])
        hit_target = hi >= target
        hit_stop = lo <= stop_loss
        if hit_target and hit_stop:
            return "SL_HIT"  # ambiguous same-bar touch — conservative, documented above
        if hit_target:
            return "TARGET_HIT"
        if hit_stop:
            return "SL_HIT"

    if len(forward) >= horizon_bars:
        return "TIMEOUT"
    return "UNRESOLVED"  # still within the horizon window, not enough forward data yet


def reconcile_shadow_log(
    tools: Any,
    log_path: Path = DEFAULT_LOG_PATH,
    horizon_bars: int = 48,
) -> Dict[str, Any]:
    if not log_path.exists():
        return {"available": False, "reason": f"No shadow log found at {log_path}."}

    client = getattr(tools.engine, "client", None)
    if client is None:
        return {"available": False, "reason": "No broker client attached — cannot fetch candles to reconcile outcomes."}

    with open(log_path) as f:
        rows = list(csv.DictReader(f))

    resolved, skipped, unresolved, still_pending = 0, 0, 0, 0

    for row in rows:
        if row.get("hypothetical_outcome"):
            skipped += 1
            continue

        instrument_key = row.get("instrument_key")
        try:
            entry_low = float(row["entry_low"]) if row.get("entry_low") else None
            entry_high = float(row["entry_high"]) if row.get("entry_high") else None
            stop_loss = float(row["stop_loss"]) if row.get("stop_loss") else None
            target = float(row["target_1"]) if row.get("target_1") else None
        except ValueError:
            entry_low = entry_high = stop_loss = target = None

        if not instrument_key or not (entry_low and entry_high and stop_loss and target):
            row["hypothetical_outcome"] = "UNRESOLVABLE_MISSING_DATA"
            unresolved += 1
            continue

        entry_mid = (entry_low + entry_high) / 2.0
        outcome = _resolve_outcome(
            client, instrument_key, row.get("timestamp", ""),
            entry_mid, stop_loss, target, horizon_bars=horizon_bars,
        )
        if outcome == "UNRESOLVED":
            still_pending += 1  # not enough time/data has passed yet — leave blank, try again later
        else:
            row["hypothetical_outcome"] = outcome
            resolved += 1

    # Rewrite the file with resolved outcomes filled in — append-only for
    # NEW rows (shadow_logger.log_trade_plan), but reconciliation is the
    # one process allowed to fill in the one blank column of past rows.
    with open(log_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in FIELDS})

    return {
        "available": True,
        "log_path": str(log_path),
        "total_rows": len(rows),
        "newly_resolved": resolved,
        "already_had_outcome": skipped,
        "unresolvable_missing_data": unresolved,
        "still_pending": still_pending,
    }


async def reconcile_forever(
    tools: Any,
    interval_seconds: float = 300.0,
    log_path: Path = DEFAULT_LOG_PATH,
    horizon_bars: int = 48,
) -> None:
    """Periodic reconciliation job, mirroring LiveScanner.run_forever()'s
    own pattern — meant to be started as a background asyncio task (e.g.
    `asyncio.create_task(reconcile_forever(tools))`) alongside the
    scanner, not run inline in a request. Never raises out of the loop —
    a single failed reconciliation pass is logged and retried next
    interval, not fatal."""
    import asyncio
    import logging
    logger = logging.getLogger(__name__)
    while True:
        try:
            result = reconcile_shadow_log(tools, log_path=log_path, horizon_bars=horizon_bars)
            if result.get("available"):
                logger.info(
                    "[copilot.reconciliation] resolved=%s pending=%s unresolvable=%s",
                    result.get("newly_resolved"), result.get("still_pending"), result.get("unresolvable_missing_data"),
                )
        except Exception as e:
            logger.warning("[copilot.reconciliation] pass failed: %s", e)
        await asyncio.sleep(interval_seconds)
