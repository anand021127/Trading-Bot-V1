"""Trading engine that coordinates orders, risk, sizing, and persistence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from backend.database.db_manager import DatabaseManager
from backend.database.models import Position, Trade
from backend.notifications.email_alerts import EmailAlerts
from backend.notifications.telegram_alerts import TelegramAlerts
from backend.orders.order_manager import OrderManager
from backend.orders.order_models import Order, OrderRequest
from backend.risk.position_sizer import PositionSizer
from backend.risk.risk_manager import RiskManager
from backend.strategy.exit_manager import ExitManager


class TradingEngine:
    """Simple strategy execution engine for the trading bot."""

    def __init__(
        self,
        order_manager: Optional[OrderManager] = None,
        db_manager: Optional[DatabaseManager] = None,
        risk_manager: Optional[RiskManager] = None,
        position_sizer: Optional[PositionSizer] = None,
        exit_manager: Optional[ExitManager] = None,
        telegram_alerts: Optional[TelegramAlerts] = None,
        email_alerts: Optional[EmailAlerts] = None,
        strategy_name: str = "default",
    ) -> None:
        self.order_manager = order_manager or OrderManager()
        self.db_manager = db_manager or DatabaseManager()
        self.risk_manager = risk_manager or RiskManager(capital=100000.0)
        self.position_sizer = position_sizer or PositionSizer(capital=100000.0)
        self.exit_manager = exit_manager or ExitManager()
        self.telegram_alerts = telegram_alerts
        self.email_alerts = email_alerts
        self.strategy_name = strategy_name

    def _trade_id(self) -> str:
        return str(uuid4())

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: Optional[float] = None,
        order_type: str = "market",
    ) -> Order:
        """Submit an order through the broker and persist the resulting trade."""
        order_request = OrderRequest(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            order_type=order_type,
        )
        order = self.order_manager.place_order(order_request)

        trade = Trade(
            id=order.id or self._trade_id(),
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price or 0.0,
            timestamp=datetime.now(timezone.utc),
            strategy=self.strategy_name,
            status=order.status,
            pnl=None,
            notes="executed via trading engine",
        )
        self.db_manager.insert_trade(trade)

        if side.lower() == "buy":
            self.db_manager.upsert_position(
                Position(
                    symbol=symbol,
                    quantity=quantity,
                    average_price=price or 0.0,
                    entry_time=trade.timestamp,
                    side="long",
                    unrealized_pnl=0.0,
                )
            )

        return order

    def list_trades(self) -> List[Trade]:
        return self.db_manager.list_trades()

    def list_positions(self) -> List[Position]:
        return self.db_manager.list_positions()

    def should_exit(self, position: dict, current_price: float) -> bool:
        return self.exit_manager.should_exit(position, current_price)

    def notify(self, message: str) -> None:
        if self.telegram_alerts is not None:
            try:
                self.telegram_alerts.send_message(message)
            except Exception:
                pass
        if self.email_alerts is not None:
            try:
                self.email_alerts.send_email("Trading Bot Alert", message)
            except Exception:
                pass
