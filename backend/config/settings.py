"""Typed application settings."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def _i(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _s(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class DatabaseSettings:
    path: str = field(default_factory=lambda: _s("DATABASE_PATH", "data/trading_bot.db"))


@dataclass
class BrokerSettings:
    """Upstox REST / WebSocket endpoints (no secrets here)."""

    base_url: str = field(
        default_factory=lambda: _s("UPSTOX_BASE_URL", "https://api.upstox.com/v2")
    )
    websocket_url: str = field(
        default_factory=lambda: _s(
            "UPSTOX_WEBSOCKET_URL",
            "wss://api.upstox.com/v3/feed/market-data-feed",
        )
    )


@dataclass
class CapitalSettings:
    total: float = field(default_factory=lambda: _f("TRADING_CAPITAL", 100000.0))
    max_allocation_per_trade: float = field(default_factory=lambda: _f("MAX_ALLOCATION_PCT", 0.18))
    # Fraction of capital held as cash buffer (overview / settings API).
    cash_buffer: float = field(default_factory=lambda: _f("CASH_BUFFER_PCT", 0.40))


@dataclass
class RiskSettings:
    max_daily_loss_pct: float = field(default_factory=lambda: _f("MAX_DAILY_LOSS_PCT", 0.02))
    max_trades_per_day: int = field(default_factory=lambda: _i("MAX_TRADES_PER_DAY", 3))
    max_concurrent_positions: int = field(default_factory=lambda: _i("MAX_CONCURRENT_POSITIONS", 1))
    max_consecutive_losses: int = field(default_factory=lambda: _i("MAX_CONSECUTIVE_LOSSES", 3))
    # Cooldown after max consecutive losses (RiskManager.pause_minutes_after_losses).
    pause_after_losses_minutes: int = field(
        default_factory=lambda: _i("PAUSE_AFTER_LOSSES_MINUTES", 30)
    )
    max_risk_per_trade_pct: float = field(default_factory=lambda: _f("RISK_PER_TRADE_PCT", 0.025))


@dataclass
class StrategySettings:
    name: str = field(default_factory=lambda: _s("TRADING_STRATEGY", ""))
    orb_window_start: str = "09:15"
    orb_window_end: str = "09:30"
    entry_window_start: str = "09:30"
    entry_window_end: str = "12:30"
    exit_all_by: str = field(default_factory=lambda: _s("EOD_SQUARE_OFF", "15:15"))


@dataclass
class IndicatorSettings:
    """Display/defaults for settings API — V8-D strategy keeps its own constants."""

    ema_fast: int = 20
    ema_slow: int = 50
    ema_trend: int = 200
    rsi_period: int = 14
    rsi_min: int = 55
    rsi_max: int = 75
    atr_period: int = 14
    choppiness_max: float = 61.8
    volume_multiplier: float = 1.5


@dataclass
class BacktestSettings:
    """Defaults for POST /api/backtest/jobs (historical settings.yaml values)."""

    start_date: str = field(default_factory=lambda: _s("BACKTEST_START_DATE", "2024-01-01"))
    end_date: str = field(default_factory=lambda: _s("BACKTEST_END_DATE", "2024-12-31"))
    commission_pct: float = field(default_factory=lambda: _f("BACKTEST_COMMISSION_PCT", 0.0003))
    slippage_pct: float = field(default_factory=lambda: _f("BACKTEST_SLIPPAGE_PCT", 0.0001))
    stt_pct: float = field(default_factory=lambda: _f("BACKTEST_STT_PCT", 0.001))


@dataclass
class OrderSettings:
    product: str = field(default_factory=lambda: _s("UPSTOX_ORDER_PRODUCT", ""))
    variety: str = "DAY"


@dataclass
class NotificationSettings:
    telegram_enabled: bool = False
    email_enabled: bool = False
    sender_email: str = field(default_factory=lambda: _s("SENDER_EMAIL", ""))
    recipient_email: str = field(default_factory=lambda: _s("RECIPIENT_EMAIL", ""))


@dataclass
class Settings:
    mode: str = field(default_factory=lambda: _s("TRADING_MODE", "paper").lower())
    capital: CapitalSettings = field(default_factory=CapitalSettings)
    risk: RiskSettings = field(default_factory=RiskSettings)
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    broker: BrokerSettings = field(default_factory=BrokerSettings)
    strategy: StrategySettings = field(default_factory=StrategySettings)
    indicators: IndicatorSettings = field(default_factory=IndicatorSettings)
    order: OrderSettings = field(default_factory=OrderSettings)
    notifications: NotificationSettings = field(default_factory=NotificationSettings)
    backtest: BacktestSettings = field(default_factory=BacktestSettings)


def load_settings() -> Settings:
    return Settings()