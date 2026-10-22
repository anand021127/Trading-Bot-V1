"""Calendar-independent dates for test fixtures.

A hard-coded expiry (e.g. "2026-09-24") silently turns into "expiry today" and then "expiry is in the
past" as the calendar moves, and the engine rightly refuses expired contracts — which made otherwise
correct tests fail on a later date. Fixtures use this relative date instead."""
from datetime import date, timedelta

FUTURE_EXPIRY = (date.today() + timedelta(days=21)).isoformat()
