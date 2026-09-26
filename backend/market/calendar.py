"""Authoritative NSE/BSE exchange session & holiday calendar service.

This is the ONE authoritative source for:
  - trading-day / holiday determination (weekends + official NSE/BSE holidays)
  - special sessions (Muhurat trading on Diwali Laxmi Pujan; Budget-day sessions)
  - market open / close / last-entry / EOD square-off times
  - expiry resolution (holiday-shifted expiry weeks)
  - trading-day enumeration for coverage math

IST (Asia/Kolkata) is the only trading timezone; every datetime passed in is
converted to IST before evaluation so an IST/UTC boundary bug can never flip a
trading day.

HOLIDAY DATA POLICY — no guessing:
  The tables below are verified against official exchange circulars and
  corroborating published calendars (zerodha.com/marketintel/holiday-calendar,
  HDFC Bank 2026 holiday table, fi.money NSE holiday list; NSE circular
  CMTR59722 for 2024). Where a holiday falls on a weekend it is still listed —
  it simply has no effect because weekends already block trading.

FAIL-CLOSED for unknown years: a year beyond the verified table raises
CalendarDataError rather than silently degrading to weekend-only checks.
Tests (and only tests) inject their own tables explicitly. When the exchanges
publish a new year's list, append it here — one place, one change.

BSE and NSE equity/F&O holiday lists are identical for every verified year
(both exchanges observe the same trading-holiday circular), so one table
serves both; the exchange parameter exists for future divergence and for the
API's self-description.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Dict, Iterable, List, Mapping, Optional, Tuple, Union
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Session constants (regular NSE/BSE equity & F&O sessions)
MARKET_OPEN = time(9, 15)
LAST_ENTRY = time(9, 45)       # intraday option entries only in the first 30 min
SQUARE_OFF = time(15, 15)      # mandatory EOD square-off
MARKET_CLOSE = time(15, 30)

_HOLIDAY_SOURCE = (
    "https://zerodha.com/marketintel/holiday-calendar/ (corroborates the "
    "official NSE/BSE circulars); NSE circular CMTR59722 for 2024"
)


class Exchange(str, Enum):
    NSE = "NSE"
    BSE = "BSE"


class CalendarDataError(RuntimeError):
    """Raised when trading-day info is requested for a year with no verified
    holiday table. Fails closed — production must never silently assume a
    day is a trading day."""


@dataclass(frozen=True)
class SessionTimes:
    """Exchange session boundaries for one date (all times IST)."""
    open: time
    last_entry: time
    square_off: time
    close: time
    special: bool = False
    name: str = "Regular session"


REGULAR_SESSION = SessionTimes(
    open=MARKET_OPEN, last_entry=LAST_ENTRY,
    square_off=SQUARE_OFF, close=MARKET_CLOSE,
)

# Muhurat sessions published to date: 18:15–19:15 IST (pre-open 18:00).
_MUHURAT = SessionTimes(
    open=time(18, 15), last_entry=time(18, 30),
    square_off=time(19, 0), close=time(19, 15),
    special=True, name="Muhurat Trading (Diwali Laxmi Pujan)",
)

_BUDGET = SessionTimes(
    open=MARKET_OPEN, last_entry=LAST_ENTRY,
    square_off=SQUARE_OFF, close=MARKET_CLOSE,
    special=True, name="Union Budget special trading session",
)


def _d(s: str) -> date:
    return date.fromisoformat(s)


class SessionCalendar:
    """Authoritative trading-day/session service for one exchange."""

    # ── Verified holiday tables (YYYY-MM-DD strings) ──────────────────
    BUILTIN_HOLIDAYS: Dict[int, frozenset] = {
        2024: frozenset({  # NSE circular CMTR59722
            "2024-01-22", "2024-01-26", "2024-03-08", "2024-03-25", "2024-03-29",
            "2024-04-11", "2024-04-17", "2024-05-01", "2024-05-20", "2024-06-17",
            "2024-07-17", "2024-08-15", "2024-10-02", "2024-11-01", "2024-11-15",
            "2024-11-20", "2024-12-25",
        }),
        2025: frozenset({  # Mahashivratri (Feb 26) … Christmas (Dec 25)
            "2025-02-26", "2025-03-14", "2025-03-31", "2025-04-10", "2025-04-14",
            "2025-04-18", "2025-05-01", "2025-08-15", "2025-08-27", "2025-10-02",
            "2025-10-21", "2025-10-22", "2025-11-05", "2025-12-25",
        }),
        2026: frozenset({  # Maharashtra municipal elections (Jan 15) … Christmas (Dec 25)
            "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31",
            "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26",
            "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24",
            "2026-12-25",
        }),
    }

    # Special trading sessions published to date (open on the named day even
    # if it is a weekend/holiday — Muhurat on Diwali Laxmi Pujan).
    SPECIAL_SESSIONS: Dict[int, Dict[date, SessionTimes]] = {
        2024: {_d("2024-11-01"): _MUHURAT},
        2025: {_d("2025-10-21"): _MUHURAT},
        2026: {_d("2026-11-08"): _MUHURAT},
    }

    # Special regular-hours sessions (Budget day — the day is otherwise a
    # normal session; recorded for transparency/audit).
    BUDGET_DAY_SESSIONS: Dict[int, frozenset] = {
        2024: frozenset({"2024-07-23"}),
        2025: frozenset({"2025-02-01"}),
        2026: frozenset(),
    }

    # Most recent verified year — used by next_trading_day() to bound search.
    MAX_VERIFIED_YEAR: int = 2026

    def __init__(
        self,
        exchange: Exchange = Exchange.NSE,
        holidays: Optional[Mapping[int, Iterable[str]]] = None,
        special_sessions: Optional[Dict[int, Dict[date, SessionTimes]]] = None,
        source: str = "builtin",
    ) -> None:
        self.exchange = exchange
        self.source = source
        if holidays is None:
            self._holidays = self.BUILTIN_HOLIDAYS
        else:
            self._holidays = {
                y: frozenset(str(x) for x in days) for y, days in holidays.items()
            }
        if special_sessions is None:
            self._special = dict(self.SPECIAL_SESSIONS)
        else:
            self._special = {
                y: dict(m) for y, m in special_sessions.items()
            }

    # ── Fail-closed year access ───────────────────────────────────────
    def _holiday_set(self, year: int) -> frozenset:
        try:
            return self._holidays[year]
        except KeyError:
            raise CalendarDataError(
                f"No verified exchange holiday table for {year}. "
                "Refusing to guess trading days — extend the calendar's "
                "BUILTIN_HOLIDAYS from the official exchange circular first."
            )

    # ── Special sessions ──────────────────────────────────────────────
    def special_session(self, d: date) -> Optional[SessionTimes]:
        """Special session times for a date, if any: Muhurat (evening
        session) or a Budget-day regular-hours Saturday session."""
        sp = self._special.get(d.year, {}).get(d)
        if sp is not None:
            return sp
        if self.is_budget_day(d):
            return SessionTimes(
                open=MARKET_OPEN, last_entry=LAST_ENTRY,
                square_off=SQUARE_OFF, close=MARKET_CLOSE,
                special=True, name="Union Budget special trading session",
            )
        return None

    def is_special_session(self, d: date) -> bool:
        return self.special_session(d) is not None

    def is_budget_day(self, d: date) -> bool:
        return d.isoformat() in self.BUDGET_DAY_SESSIONS.get(d.year, frozenset())

    # ── Trading-day determination ─────────────────────────────────────
    def is_trading_day(self, d: Union[date, datetime, str]) -> bool:
        d = _coerce_date(d)
        if self.is_special_session(d):
            return True  # e.g. Muhurat on a Sunday/holiday
        if d.weekday() >= 5:
            return False
        return d.isoformat() not in self._holiday_set(d.year)

    def is_weekend(self, d: Union[date, datetime, str]) -> bool:
        return _coerce_date(d).weekday() >= 5

    def is_holiday(self, d: Union[date, datetime, str]) -> bool:
        d = _coerce_date(d)
        if self.is_special_session(d):
            return False
        if d.weekday() >= 5:
            return False  # weekend, not a (weekday) holiday
        return d.isoformat() in self._holiday_set(d.year)

    def holiday_name(self, d: Union[date, datetime, str]) -> str:
        """Best-effort human-readable name (for logs/UI only)."""
        d = _coerce_date(d)
        sp = self.special_session(d)
        if sp:
            return sp.name
        return "exchange holiday" if self.is_holiday(d) else ""

    # ── Session times / status ────────────────────────────────────────
    def session_times(self, d: Union[date, datetime, str]) -> SessionTimes:
        d = _coerce_date(d)
        return self.special_session(d) or REGULAR_SESSION

    def session_status(
        self, dt: Optional[datetime] = None
    ) -> Tuple[str, str]:
        """Classify an IST moment into exactly one session state.

        Returns (status, description) with status one of:
          NON_TRADING_DAY | BEFORE_OPEN | OPEN | AFTER_LAST_ENTRY |
          AFTER_SQUARE_OFF | CLOSED
        Weekend/holiday days are NON_TRADING_DAY even at session hours.
        """
        dt = _ensure_ist(dt or datetime.now(IST))
        d = dt.date()
        st = self.session_times(d)
        if not self.is_trading_day(d):
            return "NON_TRADING_DAY", f"market closed: {d.isoformat()} ({self.holiday_name(d) or 'weekend'})"
        t = dt.timetz().replace(tzinfo=None)
        if t < st.open:
            return "BEFORE_OPEN", f"before session open {st.open.strftime('%H:%M')} IST ({st.name})"
        if t < st.last_entry:
            return "OPEN", f"session open — new entries allowed ({st.name})"
        if t < st.square_off:
            return "AFTER_LAST_ENTRY", f"after last-entry cutoff {st.last_entry.strftime('%H:%M')} IST — positions managed, no new entries"
        if t < st.close:
            return "AFTER_SQUARE_OFF", f"after square-off {st.square_off.strftime('%H:%M')} IST — EOD flattening"
        return "CLOSED", f"after close {st.close.strftime('%H:%M')} IST"

    def market_is_open(self, dt: Optional[datetime] = None) -> bool:
        return self.session_status(dt)[0] == "OPEN"

    def in_session_hours(self, dt: Optional[datetime] = None) -> bool:
        """Whether `dt` falls within a trading session's open→close window on
        a trading day. True for the whole session (not just the new-entry
        window) — use for scan/feed gating, not entry eligibility."""
        dt = _ensure_ist(dt or datetime.now(IST))
        d = dt.date()
        if not self.is_trading_day(d):
            return False
        st = self.session_times(d)
        t = dt.timetz().replace(tzinfo=None)
        return st.open <= t < st.close

    # ── Navigation / enumeration ──────────────────────────────────────
    def last_trading_day(self, d: Union[date, datetime, str]) -> date:
        """The most recent trading day on or before `d` (bounded by the
        verified table so a far-past probe cannot loop unverified years)."""
        cur = _coerce_date(d)
        floor = min(self._holidays.keys()) - 1 if self._holidays else cur.year - 1
        while cur.year >= floor and not self.is_trading_day(cur):
            cur -= timedelta(days=1)
        if cur.year < floor:
            raise CalendarDataError("last_trading_day walked before the earliest verified year")
        return cur

    def next_trading_day(self, d: Union[date, datetime, str]) -> date:
        """The next trading day strictly after `d` (raises past the verified
        table — no guesses about future years)."""
        cur = _coerce_date(d) + timedelta(days=1)
        ceiling = max(self._holidays.keys()) if self._holidays else cur.year
        while cur.year <= ceiling and not self.is_trading_day(cur):
            cur += timedelta(days=1)
        if cur.year > ceiling:
            raise CalendarDataError(
                f"next_trading_day walked past the latest verified year "
                f"({ceiling}) — extend the calendar tables first"
            )
        return cur

    def trading_days_between(
        self, start: Union[date, str], end: Union[date, str]
    ) -> List[date]:
        """Inclusive trading-day enumeration (deterministic, reproducible)."""
        s, e = _coerce_date(start), _coerce_date(end)
        if e < s:
            return []
        out: List[date] = []
        cur = s
        while cur <= e:
            if self.is_trading_day(cur):
                out.append(cur)
            cur += timedelta(days=1)
        return out

    def trading_days_count(self, start: Union[date, str], end: Union[date, str]) -> int:
        return len(self.trading_days_between(start, end))

    # ── Expiry resolution (holiday shift) ─────────────────────────────
    def resolve_expiry_week(
        self,
        target: Union[date, str],
        *,
        expiry_weekday: int = 3,
        symbol: str = "",
    ) -> Tuple[date, bool]:
        """Expiry date for the week containing `target` (0=Mon … 3=Thu).

        Computes the calendar weekday, then shifts BACKWARD to the previous
        trading day when the expiry lands on a non-trading day — the
        NSE/BSE rule for holiday-shifted expiries. Returns (expiry, shifted).
        Deterministic and reproducible: pure calendar math over the verified
        tables, no network, no wall-clock.
        """
        t = _coerce_date(target)
        days_ahead = (expiry_weekday - t.weekday()) % 7
        expiry = t + timedelta(days=days_ahead)
        shifted = False
        while not self.is_trading_day(expiry):
            expiry -= timedelta(days=1)
            shifted = True
        return expiry, shifted


def _coerce_date(v: Union[date, datetime, str]) -> date:
    if isinstance(v, datetime):
        return _ensure_ist(v).date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _ensure_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        # Naive datetimes in this codebase are candle/broker wall times, which
        # are IST. Interpret them as IST instead of silently assuming UTC.
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


# ── Module-level singleton + thin functional API ──────────────────────
exchange_calendar = SessionCalendar()


def is_market_open_now() -> bool:
    return exchange_calendar.market_is_open()


def current_trading_day() -> str:
    return exchange_calendar.last_trading_day(datetime.now(IST)).isoformat()


def is_trading_day_str(day_iso: str) -> bool:
    return exchange_calendar.is_trading_day(_d(day_iso))


def trading_days_count_str(start_iso: str, end_iso: str) -> int:
    return exchange_calendar.trading_days_count(_d(start_iso), _d(end_iso))


def session_status_for_timestamp(ts: Union[str, datetime]) -> Tuple[str, str]:
    """Session status for a candle/broker timestamp (ISO string or datetime).

    Candle timestamps are IST wall times; ISO strings without an offset are
    interpreted as IST, never silently as UTC.
    """
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return "UNKNOWN", f"unparseable timestamp: {ts!r}"
    else:
        dt = ts
    return exchange_calendar.session_status(dt)


__all__ = [
    "Exchange",
    "SessionCalendar",
    "SessionTimes",
    "CalendarDataError",
    "MARKET_OPEN",
    "LAST_ENTRY",
    "SQUARE_OFF",
    "MARKET_CLOSE",
    "exchange_calendar",
    "is_market_open_now",
    "current_trading_day",
    "is_trading_day_str",
    "trading_days_count_str",
    "session_status_for_timestamp",
]
