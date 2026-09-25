"""Default tests are offline. Set ALLOW_LIVE_UPSTOX=1 to opt into live calls.

Isolates global token-resolver state between tests so a prior test that
called validate_token_live / set_verified_runtime_token cannot leak a
verified runtime token into a later test that only sets os.environ.
Production priority (verified runtime > env) is preserved; only the
test process state is reset.
"""
from __future__ import annotations

import os
import tempfile
import uuid

os.environ.setdefault("PYTEST_RUNNING", "1")
# Bound native BLAS thread pools before numpy is imported anywhere in the
# test process: each OpenBLAS thread reserves large per-thread buffers and on
# small-RAM Windows hosts subprocesses spawned by e2e tests previously died
# with "OpenBLAS error: Memory allocation still failed after 10 retries".
for _blas_var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_blas_var, "1")
os.environ.setdefault("TRADING_BOT_OFFLINE_TESTS", "1")
os.environ.setdefault("TRADING_MODE", "paper")
os.environ.setdefault("TRADING_STRATEGY", "V8_D_PULLBACK_ATM")
os.environ.setdefault("UPSTOX_ORDER_PRODUCT", "I")
# Unique DB per process to avoid SQLite disk I/O collisions across tests
_db = os.path.join(tempfile.gettempdir(), f"pytest_trading_bot_{uuid.uuid4().hex}.db")
os.environ["DATABASE_PATH"] = _db
if os.environ.get("ALLOW_LIVE_UPSTOX") != "1":
    os.environ.pop("UPSTOX_ACCESS_TOKEN", None)

try:
    import pytest
except ImportError:  # unittest-only runners still import this module
    pytest = None  # type: ignore


def _clear_token_runtime_state() -> None:
    """Reset in-memory verified token; do not invent tokens or weaken production priority."""
    try:
        from backend.broker.token_resolver import clear_verified_runtime_token
        clear_verified_runtime_token()
    except Exception:
        pass


if pytest is not None:

    @pytest.fixture(autouse=True)
    def _isolate_token_resolver_state():
        """Clear verified runtime token before and after every pytest test."""
        _clear_token_runtime_state()
        yield
        _clear_token_runtime_state()
