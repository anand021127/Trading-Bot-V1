"""Strategy V8-D: Validated Production Strategy Specification (Improved).

Specifications:
- Underlying: NIFTY50, BANKNIFTY (and other indices via config)
- Entry Model: V7-G Pullback/Retest + Reversal Confirmation on Underlying Index
  (strengthened: stricter RSI, trend separation, body strength, controlled pullback depth)
- Strike Selection: Strict ATM (Nearest Round Strike)
- Option Type: CE for Bullish Pullback, PE for Bearish Pullback
- Stop Loss: Dynamic — max(28% fixed, 1.8× option ATR), capped at 40% of premium
  (widened vs original 20% to reduce premature SL hits especially on CE)
- Target: Dynamic ~1.5R of actual SL distance or +42% (whichever larger) for positive expectancy
- Risk Limit: Maximum 2.5% Account Risk
- Capital Allocation: Maximum 18% Account Equity
- Historical Lot Size: NIFTY 25, BANKNIFTY 15
- Daily Portfolio Trade Limit: Max 3 trades per day
- Next-Bar Execution Realism: Zero lookahead bias
- Uses only real historical option OHLCV (no synthetic/Black-Scholes prices)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from backend.indicators.ema import calculate_ema
from backend.indicators.rsi import calculate_rsi
from backend.indicators.atr import calculate_atr
from backend.indicators.choppiness import choppiness_index
from backend.strategy.signal import StrategySignal, SignalType, build_condition_summary
from backend.strategy.strategies.base import Strategy


@dataclass
class V8DDecisionLog:
    """Structured audit log for every signal and execution decision."""
    timestamp: str
    underlying: str
    signal_type: str
    conditions: Dict[str, bool]
    contract: Optional[Dict[str, Any]]
    risk_evaluation: Dict[str, Any]
    sizing_evaluation: Dict[str, Any]
    decision: str  # ACCEPTED / REJECTED / NO_SIGNAL
    rejection_reasons: List[str] = field(default_factory=list)


class V8DStrategy(Strategy):
    name = "V8_D_PULLBACK_ATM"
    min_candles = 50

    def __init__(
        self,
        stop_loss_pct: float = 0.28,     # Widened to -28% to reduce premature SL hits on volatile CE premiums
        target_pct: float = 0.42,        # Raised to +42% for positive expectancy (R:R ~ 1:1.5)
        max_account_risk_pct: float = 0.025,   # Slightly tighter max 2.5% risk for safety
        max_capital_alloc_pct: float = 0.18,   # Max 18% allocation
        max_daily_trades: int = 3,
        ema_fast: int = 20,
        ema_slow: int = 50,
        rsi_period: int = 14,
        use_atr_stop: bool = True,       # Prefer ATR-based stop when option ATR available
        atr_stop_mult: float = 1.8,      # SL distance = max(fixed%, atr_mult * option_atr)
    ) -> None:
        self.stop_loss_pct = stop_loss_pct
        self.target_pct = target_pct
        self.max_account_risk_pct = max_account_risk_pct
        self.max_capital_alloc_pct = max_capital_alloc_pct
        self.max_daily_trades = max_daily_trades
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period
        self.use_atr_stop = use_atr_stop
        self.atr_stop_mult = atr_stop_mult

    @staticmethod
    def get_atm_strike(spot_price: float, underlying: str) -> int:
        """Deterministic ATM strike from spot using exchange-typical steps."""
        u = (underlying or "").upper()
        if "SENSEX" in u or "BANKEX" in u or "BANKNIFTY" in u:
            step = 100
        elif "MIDCP" in u:
            step = 25
        elif "FINNIFTY" in u:
            step = 50
        elif "NIFTY" in u:
            step = 50
        else:
            step = 50
        return int(round(float(spot_price) / step) * step)

    @staticmethod
    def get_lot_size(underlying: str) -> int:
        """DEPRECATED — do not use for sizing.

        Lot size must come from contract metadata only. This helper returns 0
        so any accidental caller fails the lot_size <= 1 rejection path.
        """
        return 0

    def detect_pullback_signal(
        self,
        candles: List[Dict[str, Any]],
    ) -> Tuple[Optional[str], Dict[str, bool], Dict[str, Any]]:
        """Evaluate V7-G Pullback/Retest + Reversal on Underlying Candles.

        Bullish Pullback:
        1. Trend: EMA20 > EMA50 and Close > EMA50
        2. Pullback: Low <= EMA20 * 1.002 (touches or tests EMA20)
        3. RSI in pullback zone: 40 <= RSI <= 60
        4. Reversal Candle: Close > Open (Green candle) and Close > previous High or Close > EMA20

        Bearish Pullback:
        1. Trend: EMA20 < EMA50 and Close < EMA50
        2. Pullback: High >= EMA20 * 0.998 (tests EMA20 from below)
        3. RSI in pullback zone: 40 <= RSI <= 60
        4. Reversal Candle: Close < Open (Red candle) and Close < previous Low or Close < EMA20
        """
        if len(candles) < self.min_candles:
            return None, {"sufficient_candles": False}, {}

        closes = [float(c["close"]) for c in candles]
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]
        opens = [float(c["open"]) for c in candles]

        ema20 = calculate_ema(closes, self.ema_fast)
        ema50 = calculate_ema(closes, self.ema_slow)
        rsi = calculate_rsi(closes, self.rsi_period)

        curr_close = closes[-1]
        curr_open = opens[-1]
        curr_high = highs[-1]
        curr_low = lows[-1]
        curr_ema20 = ema20[-1]
        curr_ema50 = ema50[-1]
        curr_rsi = rsi[-1]

        prev_high = highs[-2]
        prev_low = lows[-2]

        indicators = {
            "close": curr_close,
            "ema20": curr_ema20,
            "ema50": curr_ema50,
            "rsi": curr_rsi,
        }

        # Bullish Pullback check (stricter for CE to reduce false signals & SL hits)
        # Require clear uptrend, EMA20 test, RSI not oversold/overbought, and decisive reversal
        bull_trend = curr_ema20 > curr_ema50 and curr_close > curr_ema50 and (curr_ema20 - curr_ema50) / curr_ema50 > 0.0015
        bull_pullback = curr_low <= curr_ema20 * 1.003 and curr_low >= curr_ema20 * 0.985  # controlled touch, not deep breakdown
        bull_rsi = 45.0 <= curr_rsi <= 62.0  # tighter, higher zone for CE (avoid weak/oversold bounce)
        bull_reversal = (
            (curr_close > curr_open)
            and (curr_close > prev_high or (curr_close >= curr_ema20 and (curr_close - curr_open) / max(curr_high - curr_low, 1e-6) > 0.55))
        )
        # Body strength: green candle body > 55% of range preferred

        if bull_trend and bull_pullback and bull_rsi and bull_reversal:
            conds = {
                "bull_trend": True,
                "pullback_test": True,
                "rsi_in_zone": True,
                "reversal_confirmed": True,
                "strong_body": True,
            }
            return "CE", conds, indicators

        # Bearish Pullback check (symmetric but slightly wider RSI for PE)
        bear_trend = curr_ema20 < curr_ema50 and curr_close < curr_ema50 and (curr_ema50 - curr_ema20) / curr_ema50 > 0.0015
        bear_pullback = curr_high >= curr_ema20 * 0.997 and curr_high <= curr_ema20 * 1.015
        bear_rsi = 38.0 <= curr_rsi <= 55.0
        bear_reversal = (
            (curr_close < curr_open)
            and (curr_close < prev_low or (curr_close <= curr_ema20 and (curr_open - curr_close) / max(curr_high - curr_low, 1e-6) > 0.55))
        )

        if bear_trend and bear_pullback and bear_rsi and bear_reversal:
            conds = {
                "bear_trend": True,
                "pullback_test": True,
                "rsi_in_zone": True,
                "reversal_confirmed": True,
                "strong_body": True,
            }
            return "PE", conds, indicators

        return None, {
            "bull_trend": bull_trend,
            "bear_trend": bear_trend,
            "pullback_test": bull_pullback or bear_pullback,
            "rsi_in_zone": bull_rsi or bear_rsi,
            "reversal_confirmed": bull_reversal or bear_reversal,
        }, indicators

    def calculate_position_size(
        self,
        account_equity: float,
        option_premium: float,
        lot_size: int,
        stop_loss_premium: float,
    ) -> Tuple[int, Dict[str, Any]]:
        """Compute exact risk-capped position size based on current equity.

        Returns (quantity, sizing_details).
        """
        if account_equity <= 0 or option_premium <= 0 or lot_size <= 0:
            return 0, {"error": "Invalid inputs for sizing"}

        per_unit_risk = max(1.0, abs(option_premium - stop_loss_premium))
        max_risk_rupees = account_equity * self.max_account_risk_pct
        max_alloc_rupees = account_equity * self.max_capital_alloc_pct

        lots_by_risk = int((max_risk_rupees / per_unit_risk) // lot_size)
        lots_by_alloc = int((max_alloc_rupees / (option_premium * lot_size)))

        # Strict risk-capped sizing (if minimum 1 lot violates risk or allocation, reject trade with 0 lots)
        allowed_lots = min(lots_by_risk, lots_by_alloc)
        if allowed_lots < 1:
            quantity = 0
            allowed_lots = 0
            position_value = 0.0
            total_risk = 0.0
            alloc_pct = 0.0
            risk_pct = 0.0
        else:
            quantity = allowed_lots * lot_size
            position_value = quantity * option_premium
            total_risk = quantity * per_unit_risk
            alloc_pct = (position_value / account_equity * 100.0)
            risk_pct = (total_risk / account_equity * 100.0)

        details = {
            "account_equity": account_equity,
            "option_premium": option_premium,
            "lot_size": lot_size,
            "stop_loss_premium": stop_loss_premium,
            "per_unit_risk": per_unit_risk,
            "max_risk_rupees": max_risk_rupees,
            "max_alloc_rupees": max_alloc_rupees,
            "lots_by_risk": lots_by_risk,
            "lots_by_alloc": lots_by_alloc,
            "allowed_lots": allowed_lots,
            "quantity": quantity,
            "position_value": position_value,
            "actual_allocation_pct": round(alloc_pct, 2),
            "actual_risk_pct": round(risk_pct, 2),
        }
        return quantity, details

    def evaluate(
        self,
        symbol: str,
        candles: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
    ) -> StrategySignal:
        """Standard Strategy interface implementation."""
        context = context or {}
        spot_price = float(context.get("spot_price") or (candles[-1]["close"] if candles else 0.0))
        option_chain = context.get("option_chain") or []
        account_equity = float(context.get("account_equity") or 100000.0)
        trades_today = int(context.get("trades_today") or 0)
        kill_switch = bool(context.get("kill_switch_active", False))
        reconciliation_ok = bool(context.get("reconciliation_ok", True))

        sig, _ = self.evaluate_v8d_signal(
            underlying_symbol=symbol,
            underlying_candles=candles,
            spot_price=spot_price,
            option_chain=option_chain,
            account_equity=account_equity,
            trades_today=trades_today,
            kill_switch_active=kill_switch,
            reconciliation_ok=reconciliation_ok,
        )
        return sig

    def evaluate_v8d_signal(
        self,
        underlying_symbol: str,
        underlying_candles: List[Dict[str, Any]],
        spot_price: float,
        option_chain: List[Dict[str, Any]],
        account_equity: float,
        trades_today: int = 0,
        kill_switch_active: bool = False,
        reconciliation_ok: bool = True,
    ) -> Tuple[StrategySignal, V8DDecisionLog]:
        """Full end-to-end V8-D evaluation producing a validated signal and structured decision log."""
        ts_now = datetime.now().isoformat()
        sig = StrategySignal(strategy_name=self.name, symbol=underlying_symbol)

        # 1. Check daily trade limits & system guardrails
        rejection_reasons = []
        if kill_switch_active:
            rejection_reasons.append("Kill switch is active")
        if not reconciliation_ok:
            rejection_reasons.append("Position reconciliation mismatch or pending")
        if trades_today >= self.max_daily_trades:
            rejection_reasons.append(f"Daily trade limit reached: {trades_today}/{self.max_daily_trades}")

        # 2. Detect underlying pullback
        opt_type, conds, indics = self.detect_pullback_signal(underlying_candles)
        if not opt_type:
            rejection_reasons.append("Underlying technical pullback/reversal criteria not met")
            decision_log = V8DDecisionLog(
                timestamp=ts_now,
                underlying=underlying_symbol,
                signal_type="NONE",
                conditions=conds,
                contract=None,
                risk_evaluation={"trades_today": trades_today, "reconciliation_ok": reconciliation_ok},
                sizing_evaluation={},
                decision="NO_SIGNAL",
                rejection_reasons=rejection_reasons,
            )
            sig.rejected_reasons = rejection_reasons
            sig.entry_reason = "NO TRADE — " + "; ".join(rejection_reasons)
            return sig, decision_log

        # 3. Resolve ATM Option Contract
        atm_strike = self.get_atm_strike(spot_price, underlying_symbol)
        # Lot size comes from broker contract metadata after resolution.
        # Hardcoded get_lot_size is NOT used when metadata is present.
        lot_size = 0

        candidate_contract = None
        opt_u = (opt_type or "").upper()
        nearest = None
        nearest_dist = None
        for c in option_chain:
            try:
                sk = float(c.get("strike") or 0)
            except (TypeError, ValueError):
                continue
            ot = str(c.get("option_type") or c.get("instrument_type") or "").upper()
            if ot not in ("CE", "PE"):
                # some chains use call/put
                if "CALL" in ot:
                    ot = "CE"
                elif "PUT" in ot:
                    ot = "PE"
            if ot != opt_u:
                continue
            dist = abs(sk - float(atm_strike))
            if dist < 0.01 and c.get("instrument_key"):
                candidate_contract = c
                break
            if c.get("instrument_key") and (nearest_dist is None or dist < nearest_dist):
                nearest = c
                nearest_dist = dist
        # Prefer exact ATM; else nearest same-type strike within one step (never invent keys)
        if candidate_contract is None and nearest is not None and nearest_dist is not None:
            step_guess = 100 if nearest_dist >= 75 else 50
            if nearest_dist <= step_guess + 0.01:
                candidate_contract = nearest

        if not candidate_contract or not candidate_contract.get("instrument_key"):
            rejection_reasons.append(
                f"Could not resolve ATM {opt_type} contract near strike {atm_strike} "
                f"from live option-chain (chain_size={len(option_chain or [])})"
            )
            decision_log = V8DDecisionLog(
                timestamp=ts_now,
                underlying=underlying_symbol,
                signal_type=f"BUY_{opt_type}",
                conditions=conds,
                contract=None,
                risk_evaluation={},
                sizing_evaluation={},
                decision="REJECTED",
                rejection_reasons=rejection_reasons,
            )
            sig.rejected_reasons = rejection_reasons
            sig.entry_reason = "NO TRADE — " + "; ".join(rejection_reasons)
            return sig, decision_log

        # Contract metadata is the sole source of lot_size for sizing (no hardcoded fallback).
        try:
            lot_size = int(candidate_contract.get("lot_size") or 0)
        except (TypeError, ValueError):
            lot_size = 0
        if lot_size <= 1:
            rejection_reasons.append(
                "INVALID_LOT_SIZE — contract metadata missing or invalid lot_size; "
                "refusing hardcoded fallback"
            )

        opt_ltp = float(candidate_contract.get("ltp") or candidate_contract.get("close_price") or 0.0)
        if opt_ltp <= 0.0:
            rejection_reasons.append("Option LTP missing or zero")

        # Spot price vs option price sanity check
        if abs(opt_ltp - spot_price) / spot_price < 0.01:
            rejection_reasons.append("CRITICAL: Option price equals underlying spot price (data corruption)")

        # 4. Calculate improved Stop / Target
        # Prefer ATR-based distance when option ATR is supplied in contract (reduces CE SL noise)
        option_atr = float(candidate_contract.get("option_atr") or candidate_contract.get("atr") or 0.0)
        fixed_sl_dist = opt_ltp * self.stop_loss_pct
        if self.use_atr_stop and option_atr > 0:
            atr_sl_dist = self.atr_stop_mult * option_atr
            sl_dist = max(fixed_sl_dist, atr_sl_dist)
        else:
            sl_dist = fixed_sl_dist
        # Cap SL distance so we never risk more than ~40% of premium
        sl_dist = min(sl_dist, opt_ltp * 0.40)
        stop_loss = round(max(0.05, opt_ltp - sl_dist), 2)
        # Target aims for ~1.5R of the final SL distance for positive expectancy
        target_dist = max(opt_ltp * self.target_pct, 1.5 * sl_dist)
        target = round(opt_ltp + target_dist, 2)

        # 5. Position Sizing
        qty, sizing = self.calculate_position_size(
            account_equity=account_equity,
            option_premium=opt_ltp,
            lot_size=lot_size,
            stop_loss_premium=stop_loss,
        )

        if qty <= 0:
            rejection_reasons.append("Position size calculated to 0 lots")

        # 6. Final Decision
        if rejection_reasons:
            sig.rejected_reasons = rejection_reasons
            sig.entry_reason = "NO TRADE — " + "; ".join(rejection_reasons)
            decision = "REJECTED"
        else:
            sig.signal = SignalType.BUY
            sig.entry_price = opt_ltp
            sig.stop_loss = stop_loss
            sig.target = target
            sig.confidence = 100.0
            sig.conditions = conds
            sig.indicators = {
                "selected_contract": candidate_contract,
                "underlying_spot": spot_price,
                "atm_strike": atm_strike,
                "option_type": opt_type,
                "lot_size": lot_size,
                "sizing": sizing,
            }
            sl_pct_actual = (opt_ltp - stop_loss) / opt_ltp * 100.0 if opt_ltp > 0 else 0
            tp_pct_actual = (target - opt_ltp) / opt_ltp * 100.0 if opt_ltp > 0 else 0
            sig.entry_reason = (
                f"V8-D VALIDATED ENTRY: {underlying_symbol} {opt_type} {atm_strike} @ ₹{opt_ltp:.2f} "
                f"| SL: ₹{stop_loss:.2f} (-{sl_pct_actual:.1f}%) | Target: ₹{target:.2f} (+{tp_pct_actual:.1f}%) | Qty: {qty}"
            )
            decision = "ACCEPTED"

        decision_log = V8DDecisionLog(
            timestamp=ts_now,
            underlying=underlying_symbol,
            signal_type=f"BUY_{opt_type}",
            conditions=conds,
            contract=candidate_contract,
            risk_evaluation={"trades_today": trades_today, "reconciliation_ok": reconciliation_ok, "kill_switch": kill_switch_active},
            sizing_evaluation=sizing,
            decision=decision,
            rejection_reasons=rejection_reasons,
        )

        return sig, decision_log
