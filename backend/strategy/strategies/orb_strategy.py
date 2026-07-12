"""Opening Range Breakout (ORB) Strategy.

Rules:
  1. Opening range captured from the first `orb_minutes` of the session
     (default 15 min, 9:15-9:30 IST) — computed directly from the candle
     series, not a separately-tracked external cache.
  2. Price breaks above the opening range high.
  3. Breakout candle has volume >= volume_multiplier x its recent average
     ("volume breakout" — a break on thin volume is not trusted).
  4. Index trend confirmation: the broader index (NIFTY/BANKNIFTY, passed in
     via `context['index_trend']`) must not be bearish. If no index context
     is supplied, this condition is skipped (not failed) and noted as such.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.indicators.atr import atr as calc_atr
from backend.indicators.volume import calculate_volume_ratio
from backend.strategy.signal import StrategySignal, SignalType, build_condition_summary
from backend.strategy.strategies.base import Strategy

REASON_TEXT = {
    "orb_captured": "Opening range not yet captured — need candles from market open",
    "price_above_orb_high": "Price has not broken above the opening range high",
    "volume_breakout": "Breakout volume below the required multiple of its average",
    "index_trend_ok": "Broader index trend is bearish — breakout not confirmed",
}


class ORBStrategy(Strategy):
    name = "ORB"
    min_candles = 20

    def __init__(
        self,
        orb_minutes: int = 15,
        candle_minutes: int = 1,
        atr_period: int = 14,
        atr_multiplier: float = 1.5,
        volume_lookback: int = 20,
        volume_multiplier: float = 1.5,
        min_confidence_to_trade: float = 75.0,
    ) -> None:
        self.orb_candles_count = max(1, orb_minutes // candle_minutes)
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier
        self.volume_lookback = volume_lookback
        self.volume_multiplier = volume_multiplier
        self.min_confidence_to_trade = min_confidence_to_trade

    def _capture_opening_range(self, candles: List[Dict[str, Any]]) -> Optional[Dict[str, float]]:
        """Opening range for the CURRENT trading day only.

        Bug this fixes: taking `candles[:orb_candles_count]` unconditionally
        used the first bars of the *entire dataset* (e.g. day 1 of a year-
        long backtest) as the opening range for every single day that
        followed — so ORB could only ever "break out" relative to a stale,
        arbitrary level from months earlier. This filters down to the
        calendar day of the most recent candle first, then takes that day's
        first `orb_candles_count` bars.
        """
        if not candles:
            return None
        last_day = str(candles[-1].get("timestamp", ""))[:10]
        if not last_day:
            # No usable timestamp — fall back to the old (dataset-start)
            # behavior rather than silently returning nothing.
            today_candles = candles
        else:
            today_candles = [
                c for c in candles if str(c.get("timestamp", ""))[:10] == last_day
            ]
        if len(today_candles) < self.orb_candles_count:
            return None
        session_candles = today_candles[: self.orb_candles_count]
        highs = [float(c["high"]) for c in session_candles]
        lows = [float(c["low"]) for c in session_candles]
        return {"high": max(highs), "low": min(lows)}

    def evaluate(
        self,
        symbol: str,
        candles: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
    ) -> StrategySignal:
        context = context or {}
        if len(candles) < max(self.min_candles, self.orb_candles_count + 1):
            return self._insufficient_data(symbol, len(candles))

        closes = [float(c["close"]) for c in candles]
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]
        volumes = [float(c.get("volume", 0)) for c in candles]

        orb = self._capture_opening_range(candles)
        atr_vals = calc_atr(highs, lows, closes, self.atr_period)
        vol_ratios = calculate_volume_ratio(volumes, self.volume_lookback)

        current_close = closes[-1]
        current_atr = atr_vals[-1] if atr_vals else 0.0
        current_vol_ratio = vol_ratios[-1] if vol_ratios else 0.0

        index_trend = context.get("index_trend")  # "BULLISH" | "BEARISH" | "NEUTRAL" | None

        conditions: Dict[str, bool] = {
            "orb_captured": orb is not None,
            "price_above_orb_high": bool(orb) and current_close > orb["high"],
            "volume_breakout": current_vol_ratio >= self.volume_multiplier,
        }
        if index_trend is not None:
            conditions["index_trend_ok"] = index_trend != "BEARISH"

        confidence = 100.0 * sum(conditions.values()) / len(conditions) if conditions else 0.0

        sig = StrategySignal(strategy_name=self.name, symbol=symbol, confidence=confidence)
        sig.conditions = conditions
        sig.indicators = {
            "orb_high": round(orb["high"], 2) if orb else None,
            "orb_low": round(orb["low"], 2) if orb else None,
            "atr": round(current_atr, 4),
            "volume_ratio": round(current_vol_ratio, 2),
            "index_trend": index_trend or "not supplied",
        }
        sig.rejected_reasons = [
            REASON_TEXT[name] for name, passed in conditions.items() if not passed
        ]

        # Same rationale as EMATrendStrategy: compute real entry/stop/target
        # numbers whenever the opening range exists, independent of whether
        # the signal clears the trade threshold — so a relaxed/"daily floor"
        # check has real numbers to act on instead of zeros.
        if orb is not None:
            sig.entry_price = current_close
            sig.stop_loss = round(orb["low"], 2) if orb["low"] < current_close else round(
                current_close - self.atr_multiplier * current_atr, 2
            )
            risk = current_close - sig.stop_loss
            sig.target = round(current_close + 2 * risk, 2) if risk > 0 else current_close

        if confidence >= self.min_confidence_to_trade and conditions.get("index_trend_ok", True):
            sig.signal = SignalType.BUY
            pass_label = "ALL CONDITIONS PASSED" if all(conditions.values()) else (
                f"{sig.conditions_passed}/{sig.conditions_total} CONDITIONS PASSED"
            )
            sig.entry_reason = (
                f"{pass_label} — broke ORB high {orb['high']:.2f} on "
                f"{current_vol_ratio:.2f}x volume. BUY SIGNAL GENERATED."
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
        orb = self._capture_opening_range(candles)
        if not orb:
            return None
        current_close = float(candles[-1]["close"])
        # ORB thesis is invalidated if price falls back below the range low.
        if current_close < orb["low"]:
            return "ORB_INVALIDATED — price fell back below opening range low"
        return None
