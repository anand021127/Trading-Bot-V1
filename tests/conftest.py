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
