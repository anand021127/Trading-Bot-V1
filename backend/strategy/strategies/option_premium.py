"""Option Premium Strategy.

Unlike EMA Trend / ORB (which trade the underlying), this strategy trades
the option premium itself. It needs more context than a plain candle series:

  context = {
      "spot_price": float,               # current underlying LTP
      "underlying_trend": "BULLISH"|"BEARISH"|"NEUTRAL",
      "expiry_date": "YYYY-MM-DD",        # used for expiry-day risk gating
      "option_chain": [                  # from Upstox option-chain API
          {"strike": 22000, "option_type": "CE", "instrument_key": "...",
           "oi": 45000, "bid_price": 121.5, "ask_price": 122.5,
           "delta": 0.52, "theta": -8.3, "iv": 14.2, ...},
          ...
      ],
  }

`select_contract()` does ATM strike detection + CE/PE selection — AND real
risk filtering that a professional Indian options desk actually applies
before buying premium:

  - Liquidity: rejects contracts with low open interest or a wide/missing
    bid-ask spread — an "ATM" strike nobody is quoting is a bad fill
    waiting to happen. Walks outward to the next-nearest strike that IS
    liquid, rather than blindly taking the mathematically-nearest one.
  - Theta risk: rejects contracts where time decay (theta) is large
    relative to the premium itself — buying an option that bleeds >8-10%
    of its value per day from decay alone needs a much bigger move to
    just break even.
  - Expiry-day gating: index options can move violently in the last
    session before expiry (gamma risk). By default this strategy refuses
    to open NEW positions on the expiry date itself.

None of this is invented — every field used here (`oi`, `bid_price`,
`ask_price`, `delta`, `theta`) comes straight from Upstox's option chain
response. If Upstox doesn't return Greeks for a contract, the corresponding
check is skipped (not failed) rather than blocking on data we don't have.

Rules for a BUY (of the premium):
  1. ATM contract resolved AND liquid (nearest strike to spot with
     acceptable OI/spread, direction from underlying_trend: CE for
     BULLISH, PE for BEARISH).
  2. Not expiry day (unless explicitly allowed).
  3. Premium momentum: N-bar rate of change of the premium close >= threshold.
  4. Premium is trading above its own session VWAP (buyers in control).
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from backend.indicators.atr import atr as calc_atr
from backend.indicators.vwap import vwap as calc_vwap
from backend.strategy.signal import StrategySignal, SignalType, build_condition_summary
from backend.strategy.strategies.base import Strategy

REASON_TEXT = {
    "contract_resolved": "Could not resolve a liquid ATM CE/PE contract (missing spot price, option chain, clear underlying trend, or every nearby strike failed liquidity/theta checks)",
    "not_expiry_day": "Today is this contract's expiry day — gamma risk is too high to open a new premium-buying position",
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
        min_confidence_to_trade: float = 100.0,  # options are unforgiving — require all conditions
        min_open_interest: int = 500,
        max_bid_ask_spread_pct: float = 8.0,
        max_theta_pct_of_premium: float = 10.0,
        allow_expiry_day_entries: bool = False,
        strike_search_radius: int = 5,
    ) -> None:
        self.momentum_lookback = momentum_lookback
        self.min_momentum_pct = min_momentum_pct
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier
        self.target_r_multiple = target_r_multiple
        self.min_confidence_to_trade = min_confidence_to_trade
        # Liquidity/risk filters — see module docstring.
        self.min_open_interest = min_open_interest
        self.max_bid_ask_spread_pct = max_bid_ask_spread_pct
        self.max_theta_pct_of_premium = max_theta_pct_of_premium
        self.allow_expiry_day_entries = allow_expiry_day_entries
        self.strike_search_radius = strike_search_radius

    # ── liquidity / risk checks on a single contract ──────────────────────

    def _is_liquid(self, contract: Dict[str, Any]) -> bool:
        oi = contract.get("oi")
        if oi is not None and oi < self.min_open_interest:
            return False
        bid, ask = contract.get("bid_price"), contract.get("ask_price")
        if bid is not None and ask is not None and bid > 0:
            spread_pct = (ask - bid) / bid * 100.0
            if spread_pct > self.max_bid_ask_spread_pct:
                return False
        return True

    def _theta_risk_ok(self, contract: Dict[str, Any]) -> bool:
        theta = contract.get("theta")
        premium = contract.get("ltp") or contract.get("close_price")
        if theta is None or not premium or premium <= 0:
            return True  # no data to judge — don't block on missing Greeks
        return (abs(theta) / premium * 100.0) <= self.max_theta_pct_of_premium

    def select_contract(self, context: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """ATM strike detection + CE/PE selection, filtered for real
        liquidity and theta risk. Returns the chosen contract dict, or None
        if nothing nearby clears the bar."""
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

        # Walk outward from the true ATM strike, taking the first candidate
        # (by strike distance) that actually clears liquidity + theta risk —
        # rather than blindly taking the mathematically-nearest strike even
        # if nobody's quoting it.
        candidates.sort(key=lambda c: abs(float(c["strike"]) - float(spot)))
        for contract in candidates[: max(1, self.strike_search_radius * 2 + 1)]:
            if self._is_liquid(contract) and self._theta_risk_ok(contract):
                return contract
        return None

    def _is_expiry_day(self, context: Dict[str, Any]) -> bool:
        expiry_date = context.get("expiry_date")
        if not expiry_date:
            return False  # unknown — don't block on missing data
        try:
            return date.fromisoformat(str(expiry_date)) == date.today()
        except ValueError:
            return False

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

        is_expiry_day = self._is_expiry_day(context)
        expiry_ok = self.allow_expiry_day_entries or not is_expiry_day

        if len(candles) < self.min_candles:
            insufficient = self._insufficient_data(symbol, len(candles))
            insufficient.indicators["selected_contract"] = contract
            insufficient.indicators["expiry_date"] = context.get("expiry_date")
            insufficient.conditions = {"contract_resolved": True, "not_expiry_day": expiry_ok}
            if not expiry_ok:
                insufficient.rejected_reasons.append(REASON_TEXT["not_expiry_day"])
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
            "not_expiry_day": expiry_ok,
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
            "open_interest": contract.get("oi"),
            "delta": contract.get("delta"),
            "theta": contract.get("theta"),
            "iv": contract.get("iv"),
            "is_expiry_day": is_expiry_day,
            "expiry_date": context.get("expiry_date"),
        }
        sig.rejected_reasons = [
            REASON_TEXT[name] for name, passed in conditions.items() if not passed and name in REASON_TEXT
        ]

        # Entry/stop/target computed whenever we have a valid contract,
        # regardless of whether the signal clears the trade threshold — same
        # rationale as EMATrendStrategy/ORBStrategy (real numbers for a
        # relaxed/"daily floor" check to act on, not zeros).
        if current_atr > 0:
            sig.entry_price = closes[-1]
            sig.stop_loss = round(max(0.05, closes[-1] - self.atr_multiplier * current_atr), 2)
            risk = sig.entry_price - sig.stop_loss
            sig.target = round(sig.entry_price + self.target_r_multiple * risk, 2) if risk > 0 else sig.entry_price

        if confidence >= self.min_confidence_to_trade and all(conditions.values()):
            sig.signal = SignalType.BUY
            sig.entry_reason = (
                f"ALL CONDITIONS PASSED — {contract.get('option_type')} {contract.get('strike')} "
                f"(OI {contract.get('oi', 'n/a')}) premium momentum {momentum_pct:.1f}% over {lb} bars, "
                f"above VWAP. BUY SIGNAL GENERATED."
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
        context = context or {}
        if self._is_expiry_day(context) and not self.allow_expiry_day_entries:
            # Force out of any option position by the risk cutoff on
            # expiry day itself — see TradingEngine's expiry-day square-off
            # for the actual time-based enforcement; this is the strategy-
            # level signal that it's time regardless of price action.
            return "EXPIRY_DAY_RISK_EXIT — closing ahead of extreme gamma risk into settlement"
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
