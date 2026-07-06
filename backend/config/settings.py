"""Typed application settings loaded from YAML and environment."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from backend.config.loader import load_config


class BrokerSettings(BaseModel):
    base_url: str
    websocket_url: str


class CapitalSettings(BaseModel):
    total: float
    max_allocation_per_trade: float
    cash_buffer: float


class RiskSettings(BaseModel):
    max_risk_per_trade_pct: float
    max_daily_loss_pct: float
    max_trades_per_day: int
    max_concurrent_positions: int
    max_consecutive_losses: int
    pause_after_losses_minutes: int


class StrategySettings(BaseModel):
    name: str
    timeframe_entry: str
    timeframe_trend: str
    orb_window_start: str
    orb_window_end: str
    entry_window_start: str
    entry_window_end: str
    exit_all_by: str
    no_new_trades_after: str


class IndicatorSettings(BaseModel):
    ema_fast: int
    ema_slow: int
    ema_trend: int
    rsi_period: int
    rsi_min: int
    rsi_max: int
    atr_period: int
    choppiness_period: int
    choppiness_max: float
    volume_lookback: int
    volume_multiplier: float


class FilterSettings(BaseModel):
    orb_min_width_atr_multiplier: float
    orb_max_width_atr_multiplier: float
    max_gap_up_pct: float
    avoid_round_numbers_pct: float
    min_body_pct_of_range: float
    adx_min: float


class StopLossSettings(BaseModel):
    atr_multiplier: float


class TrailingStopSettings(BaseModel):
    stage2_trigger_r: float
    stage3_trigger_r: float
    stage3_atr_multiplier: float
    stage4_trigger_r: float
    stage4_atr_multiplier: float


class UniverseSettings(BaseModel):
    index: str
    min_avg_daily_volume: int
    min_adr_pct: float
    max_stocks_in_watchlist: int


class LoggingSettings(BaseModel):
    level: str
    log_dir: str
    max_file_size_mb: int
    backup_count: int


class DatabaseSettings(BaseModel):
    path: str


class NotificationSettings(BaseModel):
    email_enabled: bool
    telegram_enabled: bool
    smtp_server: str
    smtp_port: int
    sender_email: str
    recipient_email: str


class BacktestSettings(BaseModel):
    start_date: str
    end_date: str
    commission_pct: float
    slippage_pct: float
    stt_pct: float


class APISettings(BaseModel):
    host: str
    port: int
    cors_origins: List[str]


class AppSettings(BaseModel):
    mode: str
    broker: BrokerSettings
    capital: CapitalSettings
    risk: RiskSettings
    strategy: StrategySettings
    indicators: IndicatorSettings
    filters: FilterSettings
    stop_loss: StopLossSettings
    trailing_stop: TrailingStopSettings
    universe: UniverseSettings
    logging: LoggingSettings
    database: DatabaseSettings
    notifications: NotificationSettings
    backtest: BacktestSettings
    api: APISettings
    env: Dict[str, str] = Field(default_factory=dict)


def load_settings(settings_path: Optional[Path] = None, dotenv_path: Optional[Path] = None) -> AppSettings:
    raw = load_config(settings_path=settings_path, dotenv_path=dotenv_path)
    return AppSettings(**raw)
