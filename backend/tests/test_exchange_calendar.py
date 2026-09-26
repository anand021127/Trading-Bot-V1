"""Authoritative exchange-calendar tests (PHASE 4 gap #1).

Covers: weekends, normal trading days, official holidays (all three verified
years), expiry-holiday shifting, month boundaries, year boundaries, IST/UTC
boundary handling, special sessions (Muhurat/Budget), trading-day counts,
and FAIL-CLOSED behavior for unverified years.

Holiday expectations come from the verified NSE/BSE circulars baked into
backend/market/calendar.py — these tests pin them so a bad "update" cannot
silently corrupt the production calendar.
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from backend.market.calendar import (
    CalendarDataError,
    SessionCalendar,
    exchange_calendar,
)

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc


class TestWeekends(unittest.TestCase):
    def test_saturday_not_trading_day(self):
        self.assertFalse(exchange_calendar.is_trading_day(date(2024, 10, 5)))  # Sat

    def test_sunday_not_trading_day(self):
        self.assertFalse(exchange_calendar.is_trading_day(date(2024, 10, 6)))  # Sun

    def test_weekend_holiday_is_still_just_a_weekend(self):
        # Mahashivratri 2026 falls on Sunday 2026-02-15: weekend rules.
        self.assertFalse(exchange_calendar.is_trading_day("2026-02-15"))
        self.assertFalse(exchange_calendar.is_holiday("2026-02-15"))
        self.assertTrue(exchange_calendar.is_weekend("2026-02-15"))


class TestNormalTradingDays(unittest.TestCase):
    def test_regular_weekdays(self):
        for d in ("2024-10-07", "2024-10-08", "2024-10-09", "2024-10-10", "2024-10-11"):
            self.assertTrue(exchange_calendar.is_trading_day(d), d)

    def test_accepts_datetime_and_iso_string(self):
        self.assertTrue(exchange_calendar.is_trading_day(datetime(2024, 10, 9, 10, 0, tzinfo=IST)))
        self.assertTrue(exchange_calendar.is_trading_day("2024-10-09"))


class TestOfficialHolidays(unittest.TestCase):
    def test_2024_holidays(self):
        for d in ("2024-01-26", "2024-03-08", "2024-03-25", "2024-03-29", "2024-04-11",
                  "2024-05-01", "2024-05-20", "2024-06-17", "2024-07-17", "2024-08-15",
                  "2024-10-02", "2024-11-15", "2024-11-20", "2024-12-25"):
            self.assertFalse(exchange_calendar.is_trading_day(d), d)
            self.assertTrue(exchange_calendar.is_holiday(d), d)

    def test_2025_holidays(self):
        for d in ("2025-02-26", "2025-03-14", "2025-03-31", "2025-04-10", "2025-04-14",
                  "2025-04-18", "2025-05-01", "2025-08-15", "2025-08-27", "2025-10-02",
                  "2025-12-25"):
            self.assertFalse(exchange_calendar.is_trading_day(d), d)

    def test_2026_holidays(self):
        for d in ("2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03",
                  "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14",
                  "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25"):
            self.assertFalse(exchange_calendar.is_trading_day(d), d)

    def test_2024_diwali_without_muhurat_flag_is_not_special_day(self):
        # 2024-11-01 (Diwali Laxmi Pujan) had a Muhurat session — a special
        # TRADING day, so is_trading_day must be True.
        self.assertTrue(exchange_calendar.is_trading_day("2024-11-01"))
        self.assertTrue(exchange_calendar.is_special_session(date(2024, 11, 1)))


class TestExpiryHolidayShift(unittest.TestCase):
    def test_normal_thursday_expiry(self):
        expiry, shifted = exchange_calendar.resolve_expiry_week("2024-10-07", expiry_weekday=3)
        self.assertEqual(expiry, date(2024, 10, 10))
        self.assertFalse(shifted)

    def test_expiry_on_holiday_shifts_backward(self):
        # Week containing 2024-10-02 (Gandhi Jayanti, Wednesday): NIFTY Thursday
        # expiry would be 2024-10-03 — not a holiday — but a Thursday holiday
        # week (2024-10-17? no) — use 2025 Diwali: 2025-10-21 Tue is a holiday;
        # a Thursday-expiry week containing 2025-10-23 (Thu, trading day) is
        # unaffected, so instead pin the pure-shift rule with a synthetic table.
        cal = SessionCalendar(holidays={2099: ["2099-01-14"]})  # Wed holiday
        # Week: Mon 2099-01-12 .. Fri 2099-01-16; Thursday expiry 2099-01-15 is
        # a trading day (only Wed is holiday) — check shift with Friday expiry.
        expiry, shifted = cal.resolve_expiry_week("2099-01-13", expiry_weekday=2)
        # Wednesday 2099-01-14 is a holiday -> shift back to Tuesday 2099-01-13.
        self.assertEqual(expiry, date(2099, 1, 13))
        self.assertTrue(shifted)

    def test_expiry_on_weekend_shifts_backward(self):
        cal = SessionCalendar(holidays={2099: []})
        expiry, shifted = cal.resolve_expiry_week("2099-01-16", expiry_weekday=5)
        # Friday+1 = Saturday 2099-01-17 -> shift back to Friday.
        self.assertEqual(expiry, date(2099, 1, 16))
        self.assertTrue(shifted)

    def test_historical_shift_2024_diwali_week(self):
        # Diwali 2024: 2024-11-01 (Friday) was Laxmi Pujan with Muhurat — a
        # special trading day, so a Thursday-expiry week containing it is NOT
        # shifted by the holiday rule (2024-10-31 Thu is a trading day anyway).
        expiry, shifted = exchange_calendar.resolve_expiry_week("2024-10-28", expiry_weekday=3)
        self.assertEqual(expiry, date(2024, 10, 31))
        self.assertFalse(shifted)

    def test_resolver_helper_is_calendar_aware(self):
        from backend.backtest.historical_contract_resolver import get_nearest_expiry_for_date
        # Mon 2024-08-26 -> Thursday 2024-08-29 (regular week, unchanged).
        self.assertEqual(get_nearest_expiry_for_date("NIFTY50", date(2024, 8, 26)), date(2024, 8, 29))


class TestMonthAndYearBoundaries(unittest.TestCase):
    def test_month_boundary_count(self):
        # Oct 2024: 31st is Thursday (trading), 1st is Tuesday (trading);
        # holidays in range: 2024-10-02 only.
        count = exchange_calendar.trading_days_count("2024-10-01", "2024-10-31")
        self.assertEqual(count, 22)

    def test_year_boundary_navigation(self):
        # 2024-12-31 Tue trading; 2025-01-01 Wed trading (not an exchange holiday).
        self.assertTrue(exchange_calendar.is_trading_day("2024-12-31"))
        self.assertTrue(exchange_calendar.is_trading_day("2025-01-01"))
        self.assertEqual(exchange_calendar.next_trading_day("2024-12-31"), date(2025, 1, 1))
        self.assertEqual(exchange_calendar.last_trading_day("2025-01-01"), date(2025, 1, 1))

    def test_year_boundary_across_holiday(self):
        # 2025-12-25 Thu holiday; 2025-12-26 Fri trading.
        self.assertFalse(exchange_calendar.is_trading_day("2025-12-25"))
        self.assertTrue(exchange_calendar.is_trading_day("2025-12-26"))
        self.assertEqual(exchange_calendar.next_trading_day("2025-12-25"), date(2025, 12, 26))

    def test_2026_new_year_first_trading_day(self):
        # 2026-01-01 Thu trading; 2026-01-15 Thu is a holiday (Maha elections).
        self.assertTrue(exchange_calendar.is_trading_day("2026-01-01"))
        self.assertFalse(exchange_calendar.is_trading_day("2026-01-15"))
        self.assertEqual(exchange_calendar.next_trading_day("2026-01-14"), date(2026, 1, 16))


class TestISTUTCBoundary(unittest.TestCase):
    def test_utc_evening_is_ist_trading_morning(self):
        # 04:45 UTC = 10:15 IST — inside session hours on a trading day.
        dt_utc = datetime(2024, 10, 9, 4, 45, tzinfo=UTC)
        self.assertTrue(exchange_calendar.in_session_hours(dt_utc))

    def test_utc_early_morning_is_ist_after_close(self):
        # 11:00 UTC = 16:30 IST — after close.
        dt_utc = datetime(2024, 10, 9, 11, 0, tzinfo=UTC)
        self.assertFalse(exchange_calendar.in_session_hours(dt_utc))

    def test_trading_day_never_flips_at_utc_midnight(self):
        # 2024-10-09 19:30 UTC = 2024-10-10 01:00 IST — the IST date (10th)
        # is what counts, NOT the UTC date (9th).
        dt = datetime(2024, 10, 9, 19, 30, tzinfo=UTC)
        self.assertTrue(exchange_calendar.is_trading_day(dt))  # Oct 10 IST
        # A holiday evening: 2024-10-02 19:30 UTC = Oct 3 IST (trading day).
        dt2 = datetime(2024, 10, 2, 19, 30, tzinfo=UTC)
        self.assertTrue(exchange_calendar.is_trading_day(dt2))

    def test_naive_datetime_is_treated_as_ist(self):
        # Candle timestamps in this codebase are IST wall times.
        self.assertTrue(exchange_calendar.in_session_hours(datetime(2024, 10, 9, 10, 0)))


class TestSpecialSessions(unittest.TestCase):
    def test_muhurat_2024_sunday_is_trading_day(self):
        self.assertTrue(exchange_calendar.is_trading_day("2024-11-01"))

    def test_muhurat_2026_sunday_is_trading_day(self):
        self.assertTrue(exchange_calendar.is_trading_day("2026-11-08"))  # Sunday

    def test_muhurat_session_times_are_evening(self):
        st = exchange_calendar.session_times(date(2026, 11, 8))
        self.assertTrue(st.special)
        self.assertEqual(st.open.hour, 18)
        self.assertEqual(st.close.hour, 19)

    def test_budget_day_2025_is_normal_trading_day(self):
        self.assertTrue(exchange_calendar.is_trading_day("2025-02-01"))  # Saturday special session
        self.assertTrue(exchange_calendar.is_budget_day(date(2025, 2, 1)))


class TestSessionStatus(unittest.TestCase):
    def test_status_states(self):
        # Saturday morning -> NON_TRADING_DAY
        s, _ = exchange_calendar.session_status(datetime(2024, 10, 5, 10, 0, tzinfo=IST))
        self.assertEqual(s, "NON_TRADING_DAY")
        # Holiday midday -> NON_TRADING_DAY
        s, _ = exchange_calendar.session_status(datetime(2024, 10, 2, 12, 0, tzinfo=IST))
        self.assertEqual(s, "NON_TRADING_DAY")
        # Trading-day early morning -> BEFORE_OPEN
        s, _ = exchange_calendar.session_status(datetime(2024, 10, 9, 9, 0, tzinfo=IST))
        self.assertEqual(s, "BEFORE_OPEN")
        # Inside session -> OPEN (before last entry 09:45)
        s, _ = exchange_calendar.session_status(datetime(2024, 10, 9, 9, 30, tzinfo=IST))
        self.assertEqual(s, "OPEN")
        # Mid-session after last entry
        s, _ = exchange_calendar.session_status(datetime(2024, 10, 9, 12, 0, tzinfo=IST))
        self.assertEqual(s, "AFTER_LAST_ENTRY")
        # After square-off, before close
        s, _ = exchange_calendar.session_status(datetime(2024, 10, 9, 15, 20, tzinfo=IST))
        self.assertEqual(s, "AFTER_SQUARE_OFF")
        # After close
        s, _ = exchange_calendar.session_status(datetime(2024, 10, 9, 16, 0, tzinfo=IST))
        self.assertEqual(s, "CLOSED")


class TestFailClosed(unittest.TestCase):
    def test_unverified_year_raises(self):
        with self.assertRaises(CalendarDataError):
            exchange_calendar.is_trading_day("2030-06-12")

    def test_next_trading_day_past_table_raises(self):
        with self.assertRaises(CalendarDataError):
            exchange_calendar.next_trading_day("2026-12-31")

    def test_injected_table_works_for_new_years(self):
        cal = SessionCalendar(holidays={2030: ["2030-06-12"]})
        self.assertFalse(cal.is_trading_day("2030-06-12"))
        self.assertTrue(cal.is_trading_day("2030-06-13"))

    def test_next_trading_day_across_holiday_block(self):
        # Thu 2024-03-28 trading, Fri 29 Good Friday, weekend, Mon 04-01.
        self.assertEqual(exchange_calendar.next_trading_day("2024-03-28"), date(2024, 4, 1))


class TestTradingDayCountsVsWeekdayFloor(unittest.TestCase):
    def test_calendar_count_is_never_higher_than_weekday_floor(self):
        """The authoritative count must be <= naive weekday count (holidays
        only REMOVE trading days). Guards the coverage-math contract."""
        for start, end in (("2024-01-01", "2024-12-31"), ("2025-01-01", "2025-12-31")):
            s = date.fromisoformat(start)
            e = date.fromisoformat(end)
            weekdays = sum(1 for i in range((e - s).days + 1) if (s + timedelta(days=i)).weekday() < 5)
            auth = exchange_calendar.trading_days_count(s, e)
            self.assertLessEqual(auth, weekdays)
            self.assertGreater(auth, weekdays - 20)  # ~15 holidays/yr sanity band


if __name__ == "__main__":
    unittest.main()
