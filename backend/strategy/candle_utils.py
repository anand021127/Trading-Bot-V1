"""Completed-candle feeding for V8-D (single shared implementation).

The backtest evaluates V8-D on COMPLETED bars. A live intraday series ends with the
bar that is still forming; feeding it to a strategy whose entry is a *reversal
candle* test makes the decision depend on a half-built bar. Whether the API
returns the forming bar is unverified, so completeness is decided from the
TIMESTAMP (Upstox timestamps are bar STARTS): a bar starting at T is complete only
once now >= T + interval.

``EVAL_FORMING_CANDLE=1`` (or the legacy ``PAPER_EVAL_FORMING_CANDLE=1``) restores
the old behaviour (feed everything, forming bar included).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

_INTERVAL_SECONDS = {"1minute": 60, "3minute": 180, "5minute": 300, "15minute": 900, "30minute": 1800}


def interval_seconds(interval: str) -> int:
    return _INTERVAL_SECONDS.get(str(interval or "5minute").lower(), 300)


def _parse_ts(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def candle_is_complete(candle: Dict[str, Any], interval: str, now: datetime) -> Optional[bool]:
    """True/False, or None when the candle has no parseable timestamp."""
    ts = _parse_ts((candle or {}).get("timestamp") or (candle or {}).get("time"))
    if ts is None:
        return None
    return (now - ts.astimezone(timezone.utc)).total_seconds() >= interval_seconds(interval) - 0.5


def forming_candle_allowed() -> bool:
    return (os.environ.get("EVAL_FORMING_CANDLE", "").strip() == "1"
            or os.environ.get("PAPER_EVAL_FORMING_CANDLE", "").strip() == "1")


def completed_candles(candles: List[Dict[str, Any]], interval: str = "5minute",
                      now: Optional[datetime] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return (candles_to_evaluate, info). ``info`` = last_candle_complete,
    forming_candle_excluded, candles_evaluated."""
    now = now or datetime.now(timezone.utc)
    if not candles:
        return [], {"last_candle_complete": None, "forming_candle_excluded": False, "candles_evaluated": 0}
    complete = candle_is_complete(candles[-1], interval, now)
    if complete is False and not forming_candle_allowed():
        return candles[:-1], {"last_candle_complete": False, "forming_candle_excluded": True,
                              "candles_evaluated": len(candles) - 1}
    return candles, {"last_candle_complete": complete, "forming_candle_excluded": False,
                     "candles_evaluated": len(candles)}
