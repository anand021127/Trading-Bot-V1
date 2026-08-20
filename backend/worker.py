"""Background worker entry point for Render — OPTIONAL, not required.

The trading loop now runs in-process as a background task inside the web
service itself (see backend/api/main.py's lifespan), the same way the
live scanner does. That fixed a real bug: running the trading loop here,
as a genuinely separate OS process, meant this process's BotState was
independent of the web process's — Start/Stop/Kill on the dashboard had
no effect on whether this process actually traded.

This file still works if you deliberately want to split load back out
into a second Render service later (e.g. to isolate heavy backtests from
the live trading loop's responsiveness). If you do, remember BotState is
DB-backed specifically so two processes can agree on it — but that only
works if both services share the same persistent disk, which requires
both to be on a paid Render plan (free services can't attach disks at
all: https://render.com/docs/free).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

# Ensure backend package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    from backend.config.settings import load_settings
    from backend.database.db_manager import DatabaseManager

    settings = load_settings()
    db = DatabaseManager(db_path=settings.database.path)
    db.init_db()
    logger.info("Database initialised at %s", settings.database.path)

    try:
        from backend.strategy.trading_engine import TradingEngine, BotState
        from backend.notifications.telegram_alerts import TelegramAlerts
        from backend.notifications.email_alerts import EmailAlerts

        engine = TradingEngine(
            telegram_alerts=TelegramAlerts() if settings.notifications.telegram_enabled else None,
            email_alerts=EmailAlerts()    if settings.notifications.email_enabled    else None,
        )

        # IMPORTANT: this process does NOT auto-start the bot. BotState is
        # persisted to the shared DB on Render's disk, so Start/Stop/Kill
        # from the dashboard (hitting the separate web process) is what
        # actually controls this worker. A previous version of this file
        # called engine.start() unconditionally here, which force-set the
        # bot to "running" every time the worker restarted (every deploy),
        # completely ignoring whatever the dashboard showed — the exact
        # cause of "dashboard says not running but it actually is" (and its
        # inverse: dashboard says running but the worker was never told to
        # actually trade).
        logger.info(
            "Worker ready in mode: %s — waiting for Start via the dashboard "
            "(BotState is shared with the web process through the DB)",
            settings.mode,
        )

        if settings.mode not in ("paper", "live"):
            logger.info("Backtest mode — worker idle (API server handles requests)")
            while True:
                await asyncio.sleep(60)

        await engine.run_forever()

    except ImportError as e:
        logger.error("Import error in worker: %s — running heartbeat only", e)
        while True:
            logger.info("Worker heartbeat (trading engine unavailable)")
            await asyncio.sleep(60)
    except Exception as e:
        logger.exception("Worker fatal error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
