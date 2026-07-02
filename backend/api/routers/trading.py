"""Router for trading engine actions and status endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.config.settings import load_settings
from backend.strategy.trading_engine import TradingEngine
from backend.database.models import Position, Trade
from backend.notifications.email_alerts import EmailAlerts
from backend.notifications.telegram_alerts import TelegramAlerts
from backend.risk.position_sizer import PositionSizer
from backend.risk.risk_manager import RiskManager
from backend.strategy.exit_manager import ExitManager


class TradeExecutionRequest(BaseModel):
    symbol: str
    side: str
    quantity: int
    price: Optional[float] = None
    order_type: str = "market"


class SignalRequest(BaseModel):
    symbol: str
    signal: str
    quantity: int
    price: Optional[float] = None


router = APIRouter()
settings = load_settings()

engine = TradingEngine(
    risk_manager=RiskManager(capital=settings.capital.total),
    position_sizer=PositionSizer(capital=settings.capital.total),
    exit_manager=ExitManager(
        stop_loss_pct=settings.risk.max_risk_per_trade_pct,
        take_profit_pct=0.02,
    ),
    telegram_alerts=TelegramAlerts() if settings.notifications.telegram_enabled else None,
    email_alerts=EmailAlerts() if settings.notifications.email_enabled else None,
)


def _serialize_position(position: Position) -> Dict[str, Any]:
    entry_time = position.entry_time
    if isinstance(entry_time, datetime):
        entry_time = entry_time.isoformat()
    return {
        "symbol": position.symbol,
        "quantity": position.quantity,
        "average_price": position.average_price,
        "entry_time": str(entry_time),
        "side": position.side,
        "unrealized_pnl": position.unrealized_pnl,
    }


def _serialize_trade(trade: Trade) -> Dict[str, Any]:
    return {
        "id": trade.id,
        "symbol": trade.symbol,
        "side": trade.side,
        "quantity": trade.quantity,
        "price": trade.price,
        "timestamp": trade.timestamp.isoformat(),
        "strategy": trade.strategy,
        "status": trade.status,
        "pnl": trade.pnl,
        "notes": trade.notes,
    }


@router.get("/positions")
async def get_positions() -> List[Dict[str, Any]]:
    return [_serialize_position(position) for position in engine.list_positions()]


@router.get("/trades")
async def get_trades() -> List[Dict[str, Any]]:
    return [_serialize_trade(trade) for trade in engine.list_trades()]


@router.post("/execute")
async def execute_trade(request: TradeExecutionRequest) -> Dict[str, Any]:
    try:
        order = engine.place_order(
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            price=request.price,
            order_type=request.order_type,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "status": "executed",
        "order": {
            "id": order.id,
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
            "price": order.price,
            "order_type": order.order_type,
            "status": order.status,
        },
    }


@router.post("/signal")
async def handle_signal(request: SignalRequest) -> Dict[str, Any]:
    signal = request.signal.lower()
    if signal == "none":
        return {"status": "no_action"}
    if signal not in {"long", "short"}:
        raise HTTPException(status_code=400, detail="invalid signal")

    try:
        order = engine.place_order(
            symbol=request.symbol,
            side="buy" if signal == "long" else "sell",
            quantity=request.quantity,
            price=request.price,
            order_type="market",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "status": "executed",
        "signal": signal,
        "order": {
            "id": order.id,
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
            "price": order.price,
            "order_type": order.order_type,
            "status": order.status,
        },
    }
