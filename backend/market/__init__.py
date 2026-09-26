"""Market infrastructure package.

Contains the ONE authoritative exchange session/holiday calendar service
(backend/market/calendar.py) that all production modules use for trading-day,
session, and expiry decisions. No module may re-implement weekday/time
calculations independently.
"""
from backend.market.calendar import (
    Exchange,
    SessionCalendar,
    SessionTimes,
    CalendarDataError,
    exchange_calendar,
    is_market_open_now,
    current_trading_day,
    session_status_for_timestamp,
)

__all__ = [
    "Exchange",
    "SessionCalendar",
    "SessionTimes",
    "CalendarDataError",
    "exchange_calendar",
    "is_market_open_now",
    "current_trading_day",
    "session_status_for_timestamp",
]
