"""Option Premium Strategy.

Unlike EMA Trend / ORB (which trade the underlying), this strategy trades
the option premium itself. It needs more context than a plain candle series:

  context = {
      "spot_price": float,               # current underlying LTP
      "underlying_trend": "BULLISH"|"BEARISH"|"NEUTRAL",
      "option_chain": [                  # from Upstox option-chain API
          {"strike": 22000, "option_type": "CE", "instrument_key": "..."},
          {"strike": 22000, "option_type": "PE", "instrument_key": "..."},
          ...
      ],
  }

`select_contract()` does the ATM strike detection + CE/PE selection. The
caller is then expected to fetch that contract's own historical candles and
pass them as `candles` into `evaluate()` — this strategy then scores the
*premium's* momentum and VWAP, exactly like the others score price action.

Rules for a BUY (of the premium):
  1. ATM contract resolved (nearest strike to spot, direction from
     underlying_trend: CE for BULLISH, PE for BEARISH).
  2. Premium momentum: N-bar rate of change of the premium close >= threshold.
  3. Premium is trading above its own session VWAP (buyers in control).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from backend.indicators.atr import atr as calc_atr
from backend.indicators.vwap import vwap as calc_vwap
from backend.strategy.signal import StrategySignal, SignalType, build_condition_summary
from backend.strategy.strategies.base import Strategy

REASON_TEXT = {
    "contract_resolved": "Could not resolve an ATM CE/PE contract (missing spot price, option chain, or a clear underlying trend)",
    "momentum_ok": "Premium momentum below the required threshold",
    "vwap_confirmed": "Premium trading below its own session VWAP",
}


class OptionPremiumStrategy(Strategy):
    name = "OPTION_PREMIUM"
    min_candles = 10

    def __init__(
        self,
        momentum_lookback: int = 5,
        min_momentum_pct: float = 1.0,
        atr_period: int = 10,
        atr_multiplier: float = 1.2,
        target_r_multiple: float = 1.5,
        min_confidence_to_trade: float = 100.0,  # options are unforgiving — require all 3
    ) -> None:
        self.momentum_lookback = momentum_lookback
        self.min_momentum_pct = min_momentum_pct
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier
        self.target_r_multiple = target_r_multiple
        self.min_confidence_to_trade = min_confidence_to_trade

    def select_contract(self, context: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """ATM strike detection + CE/PE selection. Returns the chosen
        contract dict, or None if it can't be resolved from context."""
        context = context or {}
        spot = context.get("spot_price")
        chain = context.get("option_chain") or []
        trend = context.get("underlying_trend")

        if not spot or not chain or trend not in ("BULLISH", "BEARISH"):
            return None

        option_type = "CE" if trend == "BULLISH" else "PE"
        candidates = [c for c in chain if c.get("option_type") == option_type and c.get("strike")]
        if not candidates:
            return None

        atm = min(candidates, key=lambda c: abs(float(c["strike"]) - float(spot)))
        return atm

    def evaluate(
        self,
        symbol: str,
        candles: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
    ) -> StrategySignal:
        context = context or {}
        contract = self.select_contract(context)

        sig = StrategySignal(strategy_name=self.name, symbol=symbol)

        if contract is None:
            sig.conditions = {"contract_resolved": False}
            sig.rejected_reasons = [REASON_TEXT["contract_resolved"]]
            sig.entry_reason = "NO TRADE — " + build_condition_summary(sig.conditions)
            return sig

        if len(candles) < self.min_candles:
            insufficient = self._insufficient_data(symbol, len(candles))
            insufficient.indicators["selected_contract"] = contract
            return insufficient

        closes = [float(c["close"]) for c in candles]
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]
        volumes = [float(c.get("volume", 0)) for c in candles]

        lb = min(self.momentum_lookback, len(closes) - 1)
        momentum_pct = (
            (closes[-1] - closes[-1 - lb]) / closes[-1 - lb] * 100.0
            if lb > 0 and closes[-1 - lb] > 0 else 0.0
        )

        vwap_vals = calc_vwap(highs, lows, closes, volumes)
        atr_vals = calc_atr(highs, lows, closes, self.atr_period)
        current_vwap = vwap_vals[-1] if vwap_vals else closes[-1]
        current_atr = atr_vals[-1] if atr_vals else 0.0

        conditions: Dict[str, bool] = {
            "contract_resolved": True,
            "momentum_ok": momentum_pct >= self.min_momentum_pct,
            "vwap_confirmed": closes[-1] > current_vwap,
        }
        confidence = 100.0 * sum(conditions.values()) / len(conditions)

        sig.confidence = confidence
        sig.conditions = conditions
        sig.indicators = {
            "selected_contract": contract,
            "premium_ltp": closes[-1],
            "premium_momentum_pct": round(momentum_pct, 2),
            "premium_vwap": round(current_vwap, 2),
            "premium_atr": round(current_atr, 4),
        }
        sig.rejected_reasons = [
            REASON_TEXT[name] for name, passed in conditions.items() if not passed
        ]

        if confidence >= self.min_confidence_to_trade and all(conditions.values()):
            sig.signal = SignalType.BUY
            sig.entry_price = closes[-1]
            sig.stop_loss = round(max(0.05, closes[-1] - self.atr_multiplier * current_atr), 2)
            risk = sig.entry_price - sig.stop_loss
            sig.target = round(sig.entry_price + self.target_r_multiple * risk, 2) if risk > 0 else sig.entry_price
            sig.entry_reason = (
                f"ALL CONDITIONS PASSED — {contract.get('option_type')} {contract.get('strike')} "
                f"premium momentum {momentum_pct:.1f}% over {lb} bars, above VWAP. "
                f"BUY SIGNAL GENERATED."
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
        if len(candles) < 2:
            return None
        closes = [float(c["close"]) for c in candles]
        # Options decay fast — exit on two consecutive down closes below VWAP.
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]
        volumes = [float(c.get("volume", 0)) for c in candles]
        vwap_vals = calc_vwap(highs, lows, closes, volumes)
        if vwap_vals and closes[-1] < vwap_vals[-1] and closes[-1] < closes[-2]:
            return "PREMIUM_MOMENTUM_LOST — closed below VWAP on falling price"
        return None
