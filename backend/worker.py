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

        # Auto-start in the mode configured
        logger.info("Worker starting in mode: %s", settings.mode)
        engine.start()

        if settings.mode in ("paper", "live"):
            await engine.run_trading_session()
        else:
            # Backtest mode — just keep process alive for API
            logger.info("Backtest mode — worker idle (API server handles requests)")
            while True:
                await asyncio.sleep(60)

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
