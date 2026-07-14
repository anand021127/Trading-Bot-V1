"""Background worker entry point for Render.

Runs the trading engine loop independently of the API server.
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

        poll_interval_seconds = 10
        while True:
            if BotState.is_running():
                logger.info("BotState is running — entering trading session")
                try:
                    await engine.run_trading_session()
                except Exception as e:
                    logger.exception("Trading session crashed, will retry after backoff: %s", e)
                    await asyncio.sleep(30)
                logger.info("Trading session ended — back to waiting for Start")
            else:
                await asyncio.sleep(poll_interval_seconds)

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
