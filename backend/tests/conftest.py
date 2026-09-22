"""Default tests are offline. Set ALLOW_LIVE_UPSTOX=1 to opt into live calls."""
from __future__ import annotations

import os
import tempfile
import uuid

os.environ.setdefault("PYTEST_RUNNING", "1")
os.environ.setdefault("TRADING_BOT_OFFLINE_TESTS", "1")
os.environ.setdefault("TRADING_MODE", "paper")
os.environ.setdefault("TRADING_STRATEGY", "V8_D_PULLBACK_ATM")
os.environ.setdefault("UPSTOX_ORDER_PRODUCT", "I")
# Unique DB per process to avoid SQLite disk I/O collisions across tests
_db = os.path.join(tempfile.gettempdir(), f"pytest_trading_bot_{uuid.uuid4().hex}.db")
os.environ["DATABASE_PATH"] = _db
if os.environ.get("ALLOW_LIVE_UPSTOX") != "1":
    os.environ.pop("UPSTOX_ACCESS_TOKEN", None)
