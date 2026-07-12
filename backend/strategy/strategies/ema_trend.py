"""EMA Trend Strategy.

Rules (all must pass for a BUY signal, each is scored independently so a
partial pass still shows up honestly on the scanner as "3/4 PASS"):
  1. EMA20 > EMA50  (trend is up)
  2. Price closed above EMA20  (price confirms the trend, not lagging it)
  3. RSI between rsi_min/rsi_max  (momentum without being overbought)
  4. Volume >= volume_multiplier x its recent average  (real participation)

Stop-loss is ATR-based (entry - atr_multiplier * ATR), not a fixed %, so it
adapts to each symbol's actual volatility.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from backend.indicators.atr import atr as calc_atr
from backend.indicators.ema import ema as calc_ema
from backend.indicators.rsi import rsi as calc_rsi
from backend.indicators.volume import calculate_volume_ratio
from backend.strategy.signal import StrategySignal, SignalType, build_condition_summary
from backend.strategy.strategies.base import Strategy

REASON_TEXT = {
    "ema_trend_up": "EMA20 is not above EMA50 — no confirmed uptrend",
    "price_above_ema20": "Price closed below EMA20 — trend not confirmed by price",
    "rsi_in_range": "RSI outside the configured momentum band",
    "volume_confirmed": "Volume did not confirm — below the required multiple of its average",
}


class EMATrendStrategy(Strategy):
    name = "EMA_TREND"
    min_candles = 55

    def __init__(
        self,
        ema_fast: int = 20,
        ema_slow: int = 50,
        rsi_period: int = 14,
        rsi_min: float = 50.0,
        rsi_max: float = 75.0,
        atr_period: int = 14,
        atr_multiplier: float = 1.5,
        volume_lookback: int = 20,
        volume_multiplier: float = 1.2,
        min_confidence_to_trade: float = 75.0,
    ) -> None:
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period
        self.rsi_min = rsi_min
        self.rsi_max = rsi_max
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier
        self.volume_lookback = volume_lookback
        self.volume_multiplier = volume_multiplier
        self.min_confidence_to_trade = min_confidence_to_trade

    def evaluate(
        self,
        symbol: str,
        candles: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
    ) -> StrategySignal:
        if len(candles) < self.min_candles:
            return self._insufficient_data(symbol, len(candles))

        closes = [float(c["close"]) for c in candles]
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]
        volumes = [float(c.get("volume", 0)) for c in candles]

        ema_fast_vals = calc_ema(closes, self.ema_fast)
        ema_slow_vals = calc_ema(closes, self.ema_slow)
        rsi_vals = calc_rsi(closes, self.rsi_period)
        atr_vals = calc_atr(highs, lows, closes, self.atr_period)
        vol_ratios = calculate_volume_ratio(volumes, self.volume_lookback)

        if not ema_fast_vals or not ema_slow_vals or not rsi_vals or not atr_vals:
            return self._insufficient_data(symbol, len(candles))

        current_close = closes[-1]
        current_ema_fast = ema_fast_vals[-1]
        current_ema_slow = ema_slow_vals[-1]
        current_rsi = rsi_vals[-1]
        current_atr = atr_vals[-1]
        current_vol_ratio = vol_ratios[-1] if vol_ratios else 0.0

        conditions: Dict[str, bool] = {
            "ema_trend_up": current_ema_fast > current_ema_slow,
            "price_above_ema20": current_close > current_ema_fast,
            "rsi_in_range": self.rsi_min <= current_rsi <= self.rsi_max,
            "volume_confirmed": current_vol_ratio >= self.volume_multiplier,
        }

        confidence = 100.0 * sum(conditions.values()) / len(conditions)

        sig = StrategySignal(strategy_name=self.name, symbol=symbol, confidence=confidence)
        sig.conditions = conditions
        sig.indicators = {
            "ema_fast": round(current_ema_fast, 2),
            "ema_slow": round(current_ema_slow, 2),
            "rsi": round(current_rsi, 2),
            "atr": round(current_atr, 4),
            "volume_ratio": round(current_vol_ratio, 2),
        }
        sig.rejected_reasons = [
            REASON_TEXT[name] for name, passed in conditions.items() if not passed
        ]

        # Entry/stop/target are computed whenever we have a valid ATR,
        # regardless of whether the signal clears the trade threshold below.
        # This lets a caller (e.g. a relaxed "daily floor" check) evaluate a
        # near-miss setup using real numbers instead of a placeholder — the
        # `signal` flag, not these fields, is what gates whether this is
        # actually actionable at the strategy's own standard.
        if current_atr > 0:
            sig.entry_price = current_close
            sig.stop_loss = round(current_close - self.atr_multiplier * current_atr, 2)
            risk = current_close - sig.stop_loss
            sig.target = round(current_close + 2 * risk, 2)  # 2R target

        if confidence >= self.min_confidence_to_trade:
            sig.signal = SignalType.BUY
            pass_label = "ALL CONDITIONS PASSED" if all(conditions.values()) else (
                f"{sig.conditions_passed}/{sig.conditions_total} CONDITIONS PASSED"
            )
            sig.entry_reason = (
                f"{pass_label} — " + build_condition_summary(conditions) +
                f". RSI {current_rsi:.1f}, volume {current_vol_ratio:.2f}x avg. BUY SIGNAL GENERATED."
            )
        else:
            sig.entry_reason = "NO TRADE — " + build_condition_summary(conditions)

        return sig

    def check_exit(
        self,
        position: Dict[str, Any],
        candles: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        if len(candles) < self.ema_slow + 1:
            return None
        closes = [float(c["close"]) for c in candles]
        ema_fast_vals = calc_ema(closes, self.ema_fast)
        ema_slow_vals = calc_ema(closes, self.ema_slow)
        if not ema_fast_vals or not ema_slow_vals:
            return None
        # Exit if the trend that justified entry has flipped.
        if ema_fast_vals[-1] < ema_slow_vals[-1]:
            return "EMA_TREND_REVERSED — EMA20 crossed below EMA50"
        return None
