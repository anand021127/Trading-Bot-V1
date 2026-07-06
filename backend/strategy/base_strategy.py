"""Base strategy abstractions for the trading bot."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class MarketDataPoint:
    symbol: str
    close: float
    volume: float
    timestamp: str
    extras: Dict[str, Any] = None


class StrategySignal:
    LONG = "long"
    SHORT = "short"
    NONE = "none"


class BaseStrategy(ABC):
    """Abstract base class for trading strategies."""

    @abstractmethod
    def assess_market(self, data: List[MarketDataPoint]) -> str:
        """Assess the market and return a trading signal."""

    @abstractmethod
    def should_exit(self, position: Dict[str, Any], data: List[MarketDataPoint]) -> bool:
        """Determine whether an open position should be exited."""
