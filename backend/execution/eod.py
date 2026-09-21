"""End-of-day square-off helper used by paper and live."""
from __future__ import annotations

from datetime import datetime
from typing import Any, List
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def parse_hhmm(value: str) -> tuple[int, int]:
    parts = str(value).strip().split(":")
    return int(parts[0]), int(parts[1])


def is_past_square_off(now: datetime, hhmm: str = "15:15") -> bool:
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    local = now.astimezone(IST)
    h, m = parse_hhmm(hhmm)
    return (local.hour, local.minute) >= (h, m)


def square_off_bot_positions(broker_close_fn, positions: List[dict], max_retries: int = 3) -> dict:
    remaining = []
    errors = []
    for pos in positions:
        ok = False
        last = None
        for _ in range(max_retries):
            try:
                broker_close_fn(pos)
                ok = True
                break
            except Exception as exc:
                last = type(exc).__name__
        if not ok:
            remaining.append(pos)
            errors.append(last)
    return {"closed": len(positions) - len(remaining), "remaining": remaining, "errors": errors}
