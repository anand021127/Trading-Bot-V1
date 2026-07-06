"""Background worker entry point for Render worker service."""

from __future__ import annotations

import asyncio
from datetime import datetime
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from backend.config.settings import load_settings
from backend.database.db_manager import DatabaseManager
from backend.notifications.email_alerts import EmailAlerts
from backend.notifications.telegram_alerts import TelegramAlerts
from backend.orders.order_manager import OrderManager
from backend.risk.position_sizer import PositionSizer
from backend.risk.risk_manager import RiskManager
from backend.strategy.exit_manager import ExitManager
from backend.strategy.trading_engine import TradingEngine


async def run_worker_loop(engine: TradingEngine) -> None:
    while True:
        positions = engine.list_positions()
        if positions:
            print(f"[{datetime.utcnow().isoformat()}] Worker heartbeat: {len(positions)} open positions")
        else:
            print(f"[{datetime.utcnow().isoformat()}] Worker heartbeat: no open positions")
        await asyncio.sleep(60)


def build_engine(settings) -> TradingEngine:
    telegram_alerts = TelegramAlerts() if settings.notifications.telegram_enabled else None
    email_alerts = EmailAlerts() if settings.notifications.email_enabled else None

    return TradingEngine(
        order_manager=OrderManager(),
        db_manager=DatabaseManager(db_path=settings.database.path),
        risk_manager=RiskManager(capital=settings.capital.total),
        position_sizer=PositionSizer(capital=settings.capital.total),
        exit_manager=ExitManager(stop_loss_pct=settings.risk.max_risk_per_trade_pct),
        telegram_alerts=telegram_alerts,
        email_alerts=email_alerts,
        strategy_name=settings.strategy.name,
    )


def start_scheduler(engine: TradingEngine) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()

    async def heartbeat() -> None:
        open_positions = engine.list_positions()
        print(
            f"[{datetime.utcnow().isoformat()}] Worker scheduler heartbeat: {len(open_positions)} positions"
        )

    scheduler.add_job(heartbeat, IntervalTrigger(minutes=1), id="worker_heartbeat")
    scheduler.start()
    return scheduler


async def main() -> None:
    settings = load_settings()
    db_manager = DatabaseManager(db_path=settings.database.path)
    db_manager.init_db()

    engine = build_engine(settings)
    scheduler = start_scheduler(engine)

    print("Worker started in mode:", settings.mode)
    try:
        await run_worker_loop(engine)
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
