"""Intraday Session Management and Market Timing Controls.

Shared session controls across Backtest, Paper, and Live Trading engines:
1. New-entry window:
   - Market open: 09:15 IST
   - New-entry start: 09:20 IST (reject entries < 09:20 IST)
   - Midday restriction: 11:30–13:00 IST (reject new entries by default due to low-volume chop,
     with a configurable exception for exceptionally strong confirmed breakout/trend regimes)
   - Last new entry cutoff: 14:45 IST (reject new entries > 14:45 IST)
   - Mandatory square-off: 15:15 IST (all open positions must be closed by 15:15 IST; zero overnight positions)
"""
from __future__ import annotations

from datetime import datetime, time
from typing import Any, Dict, Optional, Tuple, Union


class IntradaySessionManager:
    """Manages intraday market hours, new-entry gating, and square-off rules."""

    def __init__(
        self,
        market_open: time = time(9, 15),
        entry_start: time = time(9, 20),
        midday_start: time = time(11, 30),
        midday_end: time = time(13, 0),
        last_entry: time = time(14, 45),
        mandatory_square_off: time = time(15, 15),
        market_close: time = time(15, 30),
        allow_midday_breakout_exception: bool = True,
        midday_exception_min_confidence: float = 80.0,
    ) -> None:
        self.market_open = market_open
        self.entry_start = entry_start
        self.midday_start = midday_start
        self.midday_end = midday_end
        self.last_entry = last_entry
        self.mandatory_square_off = mandatory_square_off
        self.market_close = market_close
        self.allow_midday_breakout_exception = allow_midday_breakout_exception
        self.midday_exception_min_confidence = midday_exception_min_confidence

    @staticmethod
    def parse_time(val: Union[str, datetime, time, None]) -> Optional[time]:
        """Extract time object from string timestamp, datetime, or time."""
        if val is None:
            return None
        if isinstance(val, time):
            return val
        if isinstance(val, datetime):
            return val.time()
        if isinstance(val, str):
            # Formats: '2024-10-01T11:45:00+05:30', '2024-10-01 11:45:00', '11:45:00', '11:45'
            cleaned = val.strip()
            if "T" in cleaned:
                time_part = cleaned.split("T")[1]
            elif " " in cleaned:
                time_part = cleaned.split(" ")[1]
            else:
                time_part = cleaned

            # Remove timezone offset if present
            if "+" in time_part:
                time_part = time_part.split("+")[0]
            elif "-" in time_part and len(time_part) > 8:
                time_part = time_part.split("-")[0]

            parts = time_part.split(":")
            if len(parts) >= 2:
                try:
                    hr = int(parts[0])
                    mn = int(parts[1])
                    sc = int(parts[2].split(".")[0]) if len(parts) > 2 else 0
                    return time(hr, mn, sc)
                except ValueError:
                    return None
        return None

    def is_valid_entry_time(
        self,
        timestamp: Union[str, datetime, time, None],
        setup_name: str = "",
        confidence: float = 0.0,
        is_choppy: bool = False,
    ) -> Tuple[bool, str]:
        """Evaluate whether a new trade entry is allowed at the given timestamp."""
        t = self.parse_time(timestamp)
        if t is None:
            # If timestamp cannot be parsed, allow entry by default
            return True, "VALID_TIME"

        if t < self.market_open or t >= self.market_close:
            return False, f"SESSION_MARKET_CLOSED: {t.strftime('%H:%M')} is outside trading hours (09:15 - 15:30 IST)"

        if t < self.entry_start:
            return False, f"SESSION_BEFORE_ENTRY_START: {t.strftime('%H:%M')} is before new-entry start (09:20 IST)"

        if t > self.last_entry:
            return False, f"SESSION_PAST_LAST_ENTRY: {t.strftime('%H:%M')} is after last entry cutoff (14:45 IST)"

        # Midday lull check (11:30 - 13:00 IST)
        if self.midday_start <= t < self.midday_end:
            if (
                self.allow_midday_breakout_exception
                and setup_name == "BREAKOUT_EXPANSION"
                and confidence >= self.midday_exception_min_confidence
                and not is_choppy
            ):
                return True, f"MIDDAY_EXCEPTION: High-conviction breakout ({confidence:.1f}%) allowed at {t.strftime('%H:%M')}"
            return False, f"SESSION_MIDDAY_LULL: New entries restricted during 11:30–13:00 IST lull ({t.strftime('%H:%M')})"

        return True, "VALID_ENTRY_TIME"

    def is_mandatory_square_off(self, timestamp: Union[str, datetime, time, None]) -> bool:
        """Return True if the timestamp has reached or passed mandatory square-off time (15:15 IST)."""
        t = self.parse_time(timestamp)
        if t is None:
            return False
        return t >= self.mandatory_square_off


# Shared global singleton with standard institutional parameters
session_manager = IntradaySessionManager()
