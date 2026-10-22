"""Test-isolation hygiene for the root (integration) test directory.

All tests default to offline: live Upstox HTTP is blocked by
TRADING_BOT_OFFLINE_TESTS=1 (set in backend/tests/conftest.py, which also
covers this directory as the rootdir conftest). This conftest additionally
guarantees no stale UPSTOX_ACCESS_TOKEN leaks between tests so paper workers
spawned by e2e tests never arm the real market scanner, and every suite sees
a token-free environment by default. This mirrors the hygiene
backend/tests/conftest.py already applies — it does NOT change any
production priority or weaken token validation.
"""
from __future__ import annotations

import os

# The suite must not depend on the wall-clock date. The built-in exchange holiday tables end in 2026
# and the calendar (correctly) FAILS CLOSED afterwards, which would turn every clock-using test red
# on 1 Jan 2027. Give the TEST process a stand-in table for the following years (Republic Day, 26 Jan,
# a fixed gazetted holiday). This says nothing about the real calendar: production must supply the
# exchange's official list via EXCHANGE_HOLIDAYS_<YYYY> (see .env.example / the dashboard warning).
for _year in (2027, 2028, 2029):
    os.environ.setdefault(f"EXCHANGE_HOLIDAYS_{_year}", f"{_year}-01-26")

# Bound native BLAS thread pools before numpy is imported anywhere in the
# test process — see backend/tests/conftest.py for the memory-pressure
# rationale (OpenBLAS per-thread buffers OOM subprocess spawns on
# small-RAM Windows hosts).
for _blas_var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_blas_var, "1")

try:
    import pytest
except ImportError:  # unittest-only runners still import this module
    pytest = None  # type: ignore


def _clear_stale_token() -> None:
    """Remove stale UPSTOX_ACCESS_TOKEN from os.environ between tests.

    A leaked token from one test previously armed the market scanner of the
    paper worker spawned by later e2e tests (real Upstox instrument-master
    refresh + candle backfill per tick), starving their bounded wait windows.
    """
    os.environ.pop("UPSTOX_ACCESS_TOKEN", None)


if pytest is not None:

    @pytest.fixture(autouse=True)
    def _isolate_offline_token():
        """Guarantee token-free environment before and after every test."""
        _clear_stale_token()
        yield
        _clear_stale_token()
