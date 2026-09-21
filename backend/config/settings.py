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
class CapitalSettings:
    total: float = field(default_factory=lambda: _f("TRADING_CAPITAL", 100000.0))
    max_allocation_per_trade: float = field(default_factory=lambda: _f("MAX_ALLOCATION_PCT", 0.18))


@dataclass
class RiskSettings:
    max_daily_loss_pct: float = field(default_factory=lambda: _f("MAX_DAILY_LOSS_PCT", 0.02))
    max_trades_per_day: int = field(default_factory=lambda: _i("MAX_TRADES_PER_DAY", 3))
    max_concurrent_positions: int = field(default_factory=lambda: _i("MAX_CONCURRENT_POSITIONS", 1))
    max_consecutive_losses: int = field(default_factory=lambda: _i("MAX_CONSECUTIVE_LOSSES", 3))
    max_risk_per_trade_pct: float = field(default_factory=lambda: _f("RISK_PER_TRADE_PCT", 0.025))


@dataclass
class StrategySettings:
    name: str = field(default_factory=lambda: _s("TRADING_STRATEGY", ""))
    entry_window_end: str = "12:30"
    exit_all_by: str = "15:15"


@dataclass
class OrderSettings:
    product: str = field(default_factory=lambda: _s("UPSTOX_ORDER_PRODUCT", ""))
    variety: str = "DAY"


@dataclass
class NotificationSettings:
    telegram_enabled: bool = False
    email_enabled: bool = False


@dataclass
class Settings:
    mode: str = field(default_factory=lambda: _s("TRADING_MODE", "paper").lower())
    capital: CapitalSettings = field(default_factory=CapitalSettings)
    risk: RiskSettings = field(default_factory=RiskSettings)
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    strategy: StrategySettings = field(default_factory=StrategySettings)
    order: OrderSettings = field(default_factory=OrderSettings)
    notifications: NotificationSettings = field(default_factory=NotificationSettings)


def load_settings() -> Settings:
    return Settings()
