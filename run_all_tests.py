"""Full-suite test runner (legacy entry point, now a thin pytest wrapper).

HISTORY / ROOT CAUSE: this file used to be a 250-line homegrown unittest
runner with its own partial re-implementation of pytest fixtures. Once the
repo-root `pytest.py` shim was retired (production audit C5 — it shadowed
the real pytest package), this runner became a SECOND, diverging test
framework: it did not understand real pytest fixtures (it probed a
nonexistent `_is_fixture` attribute, so `@pytest.fixture` tests were
"called directly" and crashed), silently differing from CI and from
`python -m pytest`.

There must be ONE authoritative test runner. This script now preserves the
documented `python run_all_tests.py` command (README, docs/) but delegates
to the same true pytest the CI workflow uses, after applying the same
offline/paper isolation env vars the test suite expects.

Usage:
    python run_all_tests.py            # backend/tests (default)
    python run_all_tests.py tests      # repo-root integration tests
    python run_all_tests.py tests/x.py # any pytest path/args passthrough
"""
import os
import sys

# Offline/paper isolation MUST be set before any backend import (same
# contract the old runner and the retired pytest shim enforced).
os.environ.setdefault("PYTEST_RUNNING", "1")
os.environ.setdefault("TRADING_BOT_OFFLINE_TESTS", "1")
os.environ.setdefault("OFFLINE", "1")
os.environ.setdefault("TRADING_MODE", "paper")
os.environ.setdefault("TRADING_STRATEGY", "V8_D_PULLBACK_ATM")
os.environ.setdefault("UPSTOX_ORDER_PRODUCT", "I")
os.environ.setdefault("PAPER_ALLOW_TEST_SIGNAL", "0")
if os.environ.get("ALLOW_LIVE_UPSTOX") != "1":
    os.environ["ALLOW_LIVE_UPSTOX"] = "0"
    os.environ["UPSTOX_ACCESS_TOKEN"] = ""

# Reset in-memory verified token so prior process state cannot leak into the suite.
try:
    from backend.broker.token_resolver import clear_verified_runtime_token as _clear_vrt
    _clear_vrt()
except Exception:
    pass


def main() -> int:
    import pytest

    args = sys.argv[1:]
    if not args:
        # Default: the canonical backend suite (same as CI).
        args = ["backend/tests"]
    # -p no:cacheprovider avoids pytest cache contention on OneDrive-synced
    # paths (documented in PRODUCTION_RUNBOOK troubleshooting).
    return pytest.main([*args, "-p", "no:cacheprovider"])


if __name__ == "__main__":
    sys.exit(main())
