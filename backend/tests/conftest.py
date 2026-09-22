"""Default tests are offline. Set ALLOW_LIVE_UPSTOX=1 to opt into live calls."""
from __future__ import annotations

import os

os.environ.setdefault("PYTEST_RUNNING", "1")
os.environ.setdefault("TRADING_BOT_OFFLINE_TESTS", "1")
os.environ.setdefault("TRADING_MODE", "paper")
os.environ.setdefault("TRADING_STRATEGY", "V8_D_PULLBACK_ATM")
os.environ.setdefault("UPSTOX_ORDER_PRODUCT", "I")
os.environ.setdefault("DATABASE_PATH", "/tmp/pytest_trading_bot.db")
# Never inherit a real token into the default unit/integration suite.
if os.environ.get("ALLOW_LIVE_UPSTOX") != "1":
    os.environ.pop("UPSTOX_ACCESS_TOKEN", None)
