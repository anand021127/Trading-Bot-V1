"""Multi-Condition Confidence Scoring and Setup Recognition Framework.

Evaluates market regime, multi-timeframe/multi-factor technical confirmation,
and scores trade opportunities on a 0–100 confidence scale across three
high-probability option-buying setup modes:

1. MOMENTUM_CONTINUATION: Strong directional trend, VWAP expansion, RSI momentum band.
2. PULLBACK_RETEST: Healthy trend with controlled pullback to key EMA/VWAP support and reversal confirmation.
3. BREAKOUT_EXPANSION: Volatility squeeze release, volume surge, and high-velocity breakout.

Provides unified scoring for Backtest, Paper, and Live Trading engines.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from backend.indicators.atr import calculate_atr
from backend.indicators.choppiness import choppiness_index
from backend.indicators.ema import calculate_ema, ema_slope
from backend.indicators.rsi import calculate_rsi
from backend.indicators.volume import calculate_volume_ratio
from backend.indicators.vwap import calculate_vwap


@dataclass
class SetupScoreResult:
    setup_name: str  # MOMENTUM_CONTINUATION, PULLBACK_RETEST, BREAKOUT_EXPANSION, NONE
    direction: str   # CE, PE, NONE
    confidence: float  # 0.0 - 100.0
    conditions: Dict[str, bool] = field(default_factory=dict)
    factor_scores: Dict[str, float] = field(default_factory=dict)
    indicators: Dict[str, Any] = field(default_factory=dict)
    rejected_reasons: List[str] = field(default_factory=list)
    summary_text: str = ""


class ConfidenceScorer:
    """Computes weighted multi-factor technical confidence score and classifies setups."""

    def __init__(
        self,
        ema_fast: int = 20,
        ema_slow: int = 50,
        rsi_period: int = 14,
        atr_period: int = 14,
        volume_lookback: int = 20,
        ci_period: int = 14,
        min_tradeable_confidence: float = 70.0,
        max_extension_atr_mult: float = 2.5,
    ) -> None:
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period
        self.atr_period = atr_period
        self.volume_lookback = volume_lookback
        self.ci_period = ci_period
        self.min_tradeable_confidence = min_tradeable_confidence
        self.max_extension_atr_mult = max_extension_atr_mult

    def evaluate(
        self,
        candles: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
    ) -> SetupScoreResult:
        context = context or {}
        if len(candles) < 10:
            return SetupScoreResult(
                setup_name="NONE",
                direction="NONE",
                confidence=0.0,
                conditions={"sufficient_candles": False},
                rejected_reasons=["Insufficient historical bars for indicator warmup"],
                summary_text="NO TRADE — Insufficient historical data",
            )

        # Slice the most recent window for fast indicator computation
        eval_candles = candles[-120:] if len(candles) > 120 else candles

        closes = [float(c["close"]) for c in eval_candles]
        highs = [float(c["high"]) for c in eval_candles]
        lows = [float(c["low"]) for c in eval_candles]
        opens = [float(c["open"]) for c in eval_candles]
        volumes = [float(c.get("volume", 0)) for c in eval_candles]

        fast_p = min(self.ema_fast, max(3, len(closes) // 2))
        slow_p = min(self.ema_slow, max(5, len(closes) - 1))
        rsi_p = min(self.rsi_period, max(2, len(closes) - 2))
        atr_p = min(self.atr_period, max(2, len(closes) - 2))
        ci_p = min(self.ci_period, max(2, len(closes) - 2))
        vol_lb = min(self.volume_lookback, max(2, len(closes) - 1))

        # 1. Compute Indicators
        ema20 = calculate_ema(closes, fast_p)
        ema50 = calculate_ema(closes, slow_p)
        rsi_vals = calculate_rsi(closes, rsi_p)
        atr_vals = calculate_atr(highs, lows, closes, atr_p)
        vwap_vals = calculate_vwap(highs, lows, closes, volumes)
        ci_vals = choppiness_index(highs, lows, closes, ci_p)
        vol_ratios = calculate_volume_ratio(volumes, vol_lb)

        c_close = closes[-1]
        c_open = opens[-1]
        c_high = highs[-1]
        c_low = lows[-1]
        prev_close = closes[-2]
        prev_high = highs[-2]
        prev_low = lows[-2]

        c_ema20 = ema20[-1] if ema20 else c_close
        c_ema50 = ema50[-1] if ema50 else c_close
        c_rsi = rsi_vals[-1] if rsi_vals else 50.0
        c_atr = atr_vals[-1] if atr_vals else 0.0
        c_vwap = vwap_vals[-1] if vwap_vals else c_close
        c_ci = ci_vals[-1] if ci_vals else 50.0
        c_vol_ratio = vol_ratios[-1] if vol_ratios else 1.0

        # Rate of change over 1, 3 and 5 bars for momentum acceleration
        lb1 = 1
        roc_1bar = (c_close - closes[-2]) / closes[-2] * 100.0 if len(closes) > 1 and closes[-2] > 0 else 0.0
        lb3 = min(3, len(closes) - 1)
        roc_3bar = (c_close - closes[-1 - lb3]) / closes[-1 - lb3] * 100.0 if lb3 > 0 and closes[-1 - lb3] > 0 else 0.0
        lb5 = min(5, len(closes) - 1)
        roc_5bar = (c_close - closes[-1 - lb5]) / closes[-1 - lb5] * 100.0 if lb5 > 0 and closes[-1 - lb5] > 0 else 0.0

        # Momentum acceleration: is short-term momentum accelerating?
        mom_accelerating_ce = roc_1bar > 0 and (roc_3bar >= roc_5bar or roc_1bar >= (roc_3bar / 3.0))
        mom_accelerating_pe = roc_1bar < 0 and (roc_3bar <= roc_5bar or roc_1bar <= (roc_3bar / 3.0))

        ema20_slope = ema_slope(ema20, 3)

        # Distance from EMA20 in ATR multiples (Overextension check)
        dist_ema20_atr = abs(c_close - c_ema20) / c_atr if c_atr > 0 else 0.0
        is_overextended = dist_ema20_atr > self.max_extension_atr_mult

        indicators_dump = {
            "spot_price": c_close,
            "ema20": round(c_ema20, 2),
            "ema50": round(c_ema50, 2),
            "rsi": round(c_rsi, 2),
            "atr": round(c_atr, 4),
            "vwap": round(c_vwap, 2),
            "choppiness_index": round(c_ci, 2),
            "volume_ratio": round(c_vol_ratio, 2),
            "roc_1bar": round(roc_1bar, 2),
            "roc_3bar": round(roc_3bar, 2),
            "roc_5bar": round(roc_5bar, 2),
            "ema20_slope": round(ema20_slope, 4),
            "dist_ema20_atr": round(dist_ema20_atr, 2),
            "is_overextended": is_overextended,
        }

        # 2. Choppiness Regime Filter (CI > 61.8 indicates sideways chop)
        is_choppy = c_ci > 61.8

        # 3. Directional Intent & Trend Alignment
        ctx_trend = context.get("underlying_trend")
        
        bullish_structure = (c_ema20 >= c_ema50) and (c_close >= c_ema20 or c_close > c_ema50) and (ema20_slope >= -0.05)
        bearish_structure = (c_ema20 <= c_ema50) and (c_close <= c_ema20 or c_close < c_ema50) and (ema20_slope <= 0.05)

        # Reconcile with context if provided
        if ctx_trend == "BULLISH":
            primary_direction = "CE"
        elif ctx_trend == "BEARISH":
            primary_direction = "PE"
        elif bullish_structure and not bearish_structure:
            primary_direction = "CE"
        elif bearish_structure and not bullish_structure:
            primary_direction = "PE"
        else:
            primary_direction = "NONE"

        # Check for breakout candidates even during chop
        recent_highs = highs[-6:-1] if len(highs) >= 6 else highs[:-1]
        recent_lows = lows[-6:-1] if len(lows) >= 6 else lows[:-1]
        highest_prev = max(recent_highs) if recent_highs else prev_high
        lowest_prev = min(recent_lows) if recent_lows else prev_low

        is_breakout_ce = (c_close > highest_prev) and (roc_1bar >= 0.08) and (c_close > c_vwap)
        is_breakout_pe = (c_close < lowest_prev) and (roc_1bar <= -0.08) and (c_close < c_vwap)

        if is_choppy and not (is_breakout_ce or is_breakout_pe):
            reasons = [f"Choppiness Index ({c_ci:.1f}) exceeds 61.8 — market is choppy and no high-velocity breakout detected"]
            return SetupScoreResult(
                setup_name="NONE",
                direction="NONE",
                confidence=20.0,
                conditions={"trend_aligned": False, "not_choppy": False},
                factor_scores={"trend": 0.0, "momentum": 0.0, "vwap": 0.0, "volume": 0.0},
                indicators=indicators_dump,
                rejected_reasons=reasons,
                summary_text="NO TRADE — " + "; ".join(reasons),
            )

        if primary_direction == "NONE":
            if is_breakout_ce:
                primary_direction = "CE"
            elif is_breakout_pe:
                primary_direction = "PE"
            else:
                reasons = ["No clear bullish or bearish trend structure (EMA20/50 alignment neutral)"]
                return SetupScoreResult(
                    setup_name="NONE",
                    direction="NONE",
                    confidence=30.0,
                    conditions={"trend_aligned": False, "not_choppy": not is_choppy},
                    factor_scores={"trend": 0.0, "momentum": 0.0, "vwap": 0.0, "volume": 0.0},
                    indicators=indicators_dump,
                    rejected_reasons=reasons,
                    summary_text="NO TRADE — " + "; ".join(reasons),
                )

        # 4. Multi-Factor Scoring for Bullish (CE) and Bearish (PE)
        factor_scores: Dict[str, float] = {}
        conditions: Dict[str, bool] = {}
        rejected_reasons: List[str] = []

        if primary_direction == "CE":
            # --- FACTOR 1: Trend Alignment (Weight: 25) ---
            trend_score = 0.0
            if c_ema20 > c_ema50:
                trend_score += 15.0
            if c_close > c_ema20:
                trend_score += 10.0
            elif c_close > c_ema50:
                trend_score += 5.0
            factor_scores["trend"] = min(25.0, trend_score)
            conditions["trend_aligned"] = trend_score >= 15.0

            # --- FACTOR 2: Momentum, ROC & Acceleration (Weight: 30) ---
            mom_score = 0.0
            # RSI momentum band (50 to 75 ideal for bullish continuation/pullback)
            if 50.0 <= c_rsi <= 72.0:
                mom_score += 15.0
            elif 42.0 <= c_rsi < 50.0:  # healthy pullback zone
                mom_score += 12.0
            elif 72.0 < c_rsi <= 80.0:  # strong momentum thrust
                mom_score += 10.0
            elif c_rsi > 80.0:
                mom_score += 6.0

            # Short-term ROC and acceleration
            if roc_3bar >= 0.10 or roc_5bar >= 0.15:
                mom_score += 10.0
            elif roc_3bar > 0.0 or roc_5bar > 0.0:
                mom_score += 7.0
            elif c_close > c_open:
                mom_score += 5.0

            if mom_accelerating_ce:
                mom_score += 5.0

            factor_scores["momentum"] = min(30.0, mom_score)
            conditions["momentum_confirmed"] = mom_score >= 18.0

            # --- FACTOR 3: VWAP Positioning (Weight: 20) ---
            vwap_score = 0.0
            if c_close > c_vwap:
                vwap_score += 15.0
                if c_low >= c_vwap * 0.998:  # above VWAP with clean margin
                    vwap_score += 5.0
            elif c_close >= c_vwap * 0.998:  # testing/touching VWAP
                vwap_score += 10.0
            factor_scores["vwap"] = min(20.0, vwap_score)
            conditions["vwap_confirmed"] = vwap_score >= 12.0

            # --- FACTOR 4: Volume & Volatility Regime (Weight: 15) ---
            vol_score = 0.0
            if c_vol_ratio >= 1.2:
                vol_score += 9.0
            elif c_vol_ratio >= 0.9:
                vol_score += 6.0
            else:
                vol_score += 4.0

            if c_ci < 45.0:
                vol_score += 6.0
            elif c_ci <= 61.8:
                vol_score += 4.0
            factor_scores["volume_volatility"] = min(15.0, vol_score)
            conditions["volume_volatility_ok"] = vol_score >= 8.0

            # --- FACTOR 5: Setup Quality & Extension Safety (Weight: 10) ---
            setup_quality_score = 10.0
            if is_overextended:
                setup_quality_score -= 8.0  # Penalize severe overextension
                rejected_reasons.append(f"Price overextended from EMA20 ({dist_ema20_atr:.1f}x ATR > {self.max_extension_atr_mult}x)")
            factor_scores["setup_quality"] = max(0.0, setup_quality_score)
            conditions["not_overextended"] = not is_overextended

            # Setup Classification
            # Mode A: Pullback / Retest
            is_pullback = (c_low <= c_ema20 * 1.002 or c_low <= c_vwap * 1.002) and (c_close > c_open) and (40.0 <= c_rsi <= 62.0)
            # Mode B: Momentum Continuation
            is_momentum = (c_close > c_ema20) and (c_close > c_vwap) and (c_rsi >= 50.0) and (roc_3bar > 0.02)
            # Mode C: Breakout / Expansion
            is_breakout = (c_close > highest_prev) and (c_rsi >= 55.0)

            if is_pullback:
                setup_name = "PULLBACK_RETEST"
            elif is_momentum:
                setup_name = "MOMENTUM_CONTINUATION"
            elif is_breakout:
                setup_name = "BREAKOUT_EXPANSION"
            else:
                setup_name = "MOMENTUM_CONTINUATION"

        else:  # PE (Bearish)
            # --- FACTOR 1: Trend Alignment (Weight: 25) ---
            trend_score = 0.0
            if c_ema20 < c_ema50:
                trend_score += 15.0
            if c_close < c_ema20:
                trend_score += 10.0
            elif c_close < c_ema50:
                trend_score += 5.0
            factor_scores["trend"] = min(25.0, trend_score)
            conditions["trend_aligned"] = trend_score >= 15.0

            # --- FACTOR 2: Momentum, ROC & Acceleration (Weight: 30) ---
            mom_score = 0.0
            # RSI momentum band (28 to 50 ideal for bearish continuation/pullback)
            if 28.0 <= c_rsi <= 50.0:
                mom_score += 15.0
            elif 50.0 < c_rsi <= 58.0:  # healthy pullback zone to short
                mom_score += 12.0
            elif 20.0 <= c_rsi < 28.0:  # strong downward thrust
                mom_score += 10.0
            elif c_rsi < 20.0:
                mom_score += 6.0

            # Short-term ROC and acceleration
            if roc_3bar <= -0.10 or roc_5bar <= -0.15:
                mom_score += 10.0
            elif roc_3bar < 0.0 or roc_5bar < 0.0:
                mom_score += 7.0
            elif c_close < c_open:
                mom_score += 5.0

            if mom_accelerating_pe:
                mom_score += 5.0

            factor_scores["momentum"] = min(30.0, mom_score)
            conditions["momentum_confirmed"] = mom_score >= 18.0

            # --- FACTOR 3: VWAP Positioning (Weight: 20) ---
            vwap_score = 0.0
            if c_close < c_vwap:
                vwap_score += 15.0
                if c_high <= c_vwap * 1.002:
                    vwap_score += 5.0
            elif c_close <= c_vwap * 1.002:
                vwap_score += 10.0
            factor_scores["vwap"] = min(20.0, vwap_score)
            conditions["vwap_confirmed"] = vwap_score >= 12.0

            # --- FACTOR 4: Volume & Volatility Regime (Weight: 15) ---
            vol_score = 0.0
            if c_vol_ratio >= 1.2:
                vol_score += 9.0
            elif c_vol_ratio >= 0.9:
                vol_score += 6.0
            else:
                vol_score += 4.0

            if c_ci < 45.0:
                vol_score += 6.0
            elif c_ci <= 61.8:
                vol_score += 4.0
            factor_scores["volume_volatility"] = min(15.0, vol_score)
            conditions["volume_volatility_ok"] = vol_score >= 8.0

            # --- FACTOR 5: Setup Quality & Extension Safety (Weight: 10) ---
            setup_quality_score = 10.0
            if is_overextended:
                setup_quality_score -= 8.0
                rejected_reasons.append(f"Price overextended from EMA20 ({dist_ema20_atr:.1f}x ATR > {self.max_extension_atr_mult}x)")
            factor_scores["setup_quality"] = max(0.0, setup_quality_score)
            conditions["not_overextended"] = not is_overextended

            # Setup Classification
            # Mode A: Pullback / Retest
            is_pullback = (c_high >= c_ema20 * 0.998 or c_high >= c_vwap * 0.998) and (c_close < c_open) and (38.0 <= c_rsi <= 60.0)
            # Mode B: Breakout / Expansion
            is_breakout = (c_close < lowest_prev) and (c_rsi <= 48.0)
            # Mode C: Momentum Continuation
            is_momentum = (c_close < c_ema20) and (c_close < c_vwap) and (c_rsi <= 50.0) and (roc_3bar < -0.02)

            if is_pullback:
                setup_name = "PULLBACK_RETEST"
            elif is_breakout:
                setup_name = "BREAKOUT_EXPANSION"
            elif is_momentum:
                setup_name = "MOMENTUM_CONTINUATION"
            else:
                setup_name = "MOMENTUM_CONTINUATION"

        total_confidence = round(sum(factor_scores.values()), 1)

        # Check rejections
        for k, v in conditions.items():
            if not v and not any(k in r for r in rejected_reasons):
                rejected_reasons.append(f"{k.replace('_', ' ').title()} not confirmed")

        summary_text = (
            f"{setup_name} ({primary_direction}) with confidence {total_confidence:.1f}% "
            f"[Trend: {factor_scores.get('trend', 0):.0f}/25, Mom: {factor_scores.get('momentum', 0):.0f}/30, "
            f"VWAP: {factor_scores.get('vwap', 0):.0f}/20, Vol: {factor_scores.get('volume_volatility', 0):.0f}/15, "
            f"Setup: {factor_scores.get('setup_quality', 0):.0f}/10]"
        )

        return SetupScoreResult(
            setup_name=setup_name,
            direction=primary_direction,
            confidence=total_confidence,
            conditions=conditions,
            factor_scores=factor_scores,
            indicators=indicators_dump,
            rejected_reasons=rejected_reasons,
            summary_text=summary_text,
        )

