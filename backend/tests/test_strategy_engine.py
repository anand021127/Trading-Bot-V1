"""Tests for the multi-strategy engine: EMA Trend, ORB, Option Premium.

Fixtures are hand-tuned synthetic candle series with known indicator
outcomes (verified against the real indicator modules) so each assertion
checks real strategy logic, not a mocked shortcut.
"""
from __future__ import annotations

from typing import Any, Dict, List

from backend.strategy.signal import SignalType
from backend.strategy.strategies.ema_trend import EMATrendStrategy
from backend.strategy.strategies.option_premium import OptionPremiumStrategy
from backend.strategy.strategies.orb_strategy import ORBStrategy
from backend.strategy.strategy_engine import MultiStrategyEngine


def _candle(o: float, h: float, l: float, c: float, v: float, i: int) -> Dict[str, Any]:
    return {"open": o, "high": h, "low": l, "close": c, "volume": v,
            "timestamp": f"2026-01-01T09:{15 + i:02d}:00"}


def _uptrend_all_pass_candles() -> List[Dict[str, Any]]:
    """60 bars: gentle zigzag uptrend (EMA20>EMA50, RSI~61, volume spike on
    the last bar) — every EMA_TREND condition passes."""
    closes = [100.0]
    for i in range(59):
        step = 0.6 if i % 2 == 0 else -0.35
        closes.append(closes[-1] + step)
    candles = []
    for i, c in enumerate(closes):
        vol = 2500 if i == len(closes) - 1 else 1000
        candles.append(_candle(c - 0.1, c + 0.5, c - 0.5, c, vol, i))
    return candles


def _flat_choppy_candles(n: int = 60) -> List[Dict[str, Any]]:
    """Flat/choppy series — EMA20≈EMA50, should fail the trend condition."""
    closes = [100.0 + (0.2 if i % 2 == 0 else -0.2) for i in range(n)]
    return [_candle(c, c + 0.3, c - 0.3, c, 800, i) for i, c in enumerate(closes)]


class TestEMATrendStrategy:
    def test_all_conditions_pass_generates_buy(self) -> None:
        strat = EMATrendStrategy()
        sig = strat.evaluate("HDFCBANK", _uptrend_all_pass_candles())
        assert sig.signal == SignalType.BUY
        assert sig.confidence == 100.0
        assert all(sig.conditions.values())
        assert sig.rejected_reasons == []
        assert "BUY SIGNAL GENERATED" in sig.entry_reason
        assert sig.stop_loss < sig.entry_price < sig.target

    def test_choppy_market_rejects_with_reasons(self) -> None:
        strat = EMATrendStrategy()
        sig = strat.evaluate("RELIANCE", _flat_choppy_candles())
        assert sig.signal == SignalType.NONE
        assert sig.rejected_reasons  # never hidden
        assert "NO TRADE" in sig.entry_reason

    def test_insufficient_data_is_explicit(self) -> None:
        strat = EMATrendStrategy()
        sig = strat.evaluate("TCS", _uptrend_all_pass_candles()[:10])
        assert sig.signal == SignalType.NONE
        assert "Insufficient candle data" in sig.rejected_reasons[0]


class TestORBStrategy:
    def _breakout_candles(self) -> List[Dict[str, Any]]:
        # First 15 bars = opening range (9:15-9:29), high=101, low=99.
        orb = [_candle(100, 101, 99, 100, 500, i) for i in range(15)]
        # Breakout bar: closes above 101 with a volume spike.
        rest = [_candle(100.5, 102.5, 100.2, 102.2, 3000, 15)]
        # A few more bars holding above the range.
        rest += [_candle(102.2, 103, 101.8, 102.8, 1200, 16 + j) for j in range(5)]
        return orb + rest

    def test_breakout_with_volume_generates_buy(self) -> None:
        strat = ORBStrategy(volume_multiplier=1.5)
        sig = strat.evaluate("HDFCBANK", self._breakout_candles())
        assert sig.signal == SignalType.BUY
        assert sig.conditions["price_above_orb_high"] is True
        assert sig.conditions["volume_breakout"] is True
        assert sig.indicators["orb_high"] == 101.0

    def test_index_trend_bearish_blocks_signal(self) -> None:
        strat = ORBStrategy(volume_multiplier=1.5)
        sig = strat.evaluate(
            "HDFCBANK", self._breakout_candles(), context={"index_trend": "BEARISH"},
        )
        assert sig.signal == SignalType.NONE
        assert sig.conditions["index_trend_ok"] is False
        assert any("bearish" in r.lower() for r in sig.rejected_reasons)

    def test_no_breakout_yet_gives_no_trade(self) -> None:
        strat = ORBStrategy()
        flat = [_candle(100, 101, 99, 100, 500, i) for i in range(25)]
        sig = strat.evaluate("RELIANCE", flat)
        assert sig.signal == SignalType.NONE
        assert sig.conditions["price_above_orb_high"] is False

    def test_opening_range_resets_each_trading_day(self) -> None:
        """Regression test for the bug reported in production: ORB was
        anchoring to the very first day's opening range for an entire
        multi-day/multi-year dataset, so it was comparing today's price
        against a stale range from months earlier. Day 2 must use its OWN
        opening range, not day 1's."""
        def bar(ts, o, h, l, c, v):
            return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}

        candles = []
        # Day 1: opening range 100-101, stays flat (no breakout all day).
        for m in range(20):
            candles.append(bar(f"2026-01-01T09:{15+m:02d}:00", 100.2, 100.5, 99.8, 100.3, 500))
        # Day 2: gaps up — a completely different opening range, 105-106.
        for m in range(15):
            candles.append(bar(f"2026-01-02T09:{15+m:02d}:00", 105.2, 106.0, 104.8, 105.5, 500))
        # Day 2 breakout above ITS OWN range (106), with volume.
        candles.append(bar("2026-01-02T09:30:00", 105.5, 108.0, 105.3, 107.5, 3000))
        for m in range(5):
            candles.append(bar(f"2026-01-02T09:{31+m:02d}:00", 107.5, 108.5, 107.2, 108.0, 1200))

        strat = ORBStrategy(volume_multiplier=1.5)
        sig = strat.evaluate("TEST", candles)

        # The bug would have used day 1's range (100-101) — price is already
        # far above that from the very first bar of day 2, so it would look
        # like an instant "breakout" with no real signal. The fix must use
        # day 2's actual range (105-106).
        assert sig.indicators["orb_high"] == 106.0
        assert sig.signal == SignalType.BUY


class TestOptionPremiumStrategy:
    CHAIN = [
        {"strike": 22000, "option_type": "CE", "instrument_key": "NSE_FO|CE22000"},
        {"strike": 22000, "option_type": "PE", "instrument_key": "NSE_FO|PE22000"},
        {"strike": 22100, "option_type": "CE", "instrument_key": "NSE_FO|CE22100"},
    ]

    def _premium_candles_rising_above_vwap(self) -> List[Dict[str, Any]]:
        closes = [100, 101, 103, 106, 110, 115, 121, 128, 136, 145]
        vols = [1000, 1000, 1000, 1000, 1000, 1000, 1500, 2000, 2500, 3000]
        return [
            _candle(c - 1, c + 1, c - 2, c, v, i)
            for i, (c, v) in enumerate(zip(closes, vols))
        ]

    def test_select_contract_picks_atm_ce_for_bullish(self) -> None:
        strat = OptionPremiumStrategy()
        contract = strat.select_contract({
            "spot_price": 22010, "underlying_trend": "BULLISH", "option_chain": self.CHAIN,
        })
        assert contract is not None
        assert contract["strike"] == 22000
        assert contract["option_type"] == "CE"

    def test_select_contract_none_without_clear_trend(self) -> None:
        strat = OptionPremiumStrategy()
        contract = strat.select_contract({
            "spot_price": 22010, "underlying_trend": "NEUTRAL", "option_chain": self.CHAIN,
        })
        assert contract is None

    def test_momentum_and_vwap_confirmed_buy(self) -> None:
        strat = OptionPremiumStrategy(min_momentum_pct=1.0)
        context = {
            "spot_price": 22010, "underlying_trend": "BULLISH", "option_chain": self.CHAIN,
        }
        sig = strat.evaluate("NIFTY", self._premium_candles_rising_above_vwap(), context)
        assert sig.signal == SignalType.BUY
        assert sig.conditions["momentum_ok"] is True
        assert sig.conditions["vwap_confirmed"] is True
        assert sig.indicators["selected_contract"]["option_type"] == "CE"

    def test_no_chain_rejects_before_touching_candles(self) -> None:
        strat = OptionPremiumStrategy()
        sig = strat.evaluate("NIFTY", self._premium_candles_rising_above_vwap(), context={})
        assert sig.signal == SignalType.NONE
        assert "contract" in sig.rejected_reasons[0].lower()


class TestMultiStrategyEngine:
    def test_evaluate_returns_one_signal_per_strategy(self) -> None:
        engine = MultiStrategyEngine()
        candles = _uptrend_all_pass_candles()
        signals = engine.evaluate("HDFCBANK", candles, context={})
        names = {s.strategy_name for s in signals}
        assert names == {"EMA_TREND", "ORB", "OPTION_PREMIUM"}

    def test_best_signal_picks_highest_confidence_actionable(self) -> None:
        engine = MultiStrategyEngine()
        candles = _uptrend_all_pass_candles()
        signals = engine.evaluate("HDFCBANK", candles, context={})
        best = MultiStrategyEngine.best_signal(signals)
        # EMA_TREND is engineered to pass fully; OPTION_PREMIUM has no chain
        # context so it can't be actionable.
        assert best is not None
        assert best.strategy_name == "EMA_TREND"

    def test_best_signal_none_when_all_reject(self) -> None:
        engine = MultiStrategyEngine()
        candles = _flat_choppy_candles()
        signals = engine.evaluate("RELIANCE", candles, context={})
        assert MultiStrategyEngine.best_signal(signals) is None
        # transparency: every rejected strategy still explains itself
        assert all(s.rejected_reasons for s in signals)

    def test_strategy_filter_runs_subset_only(self) -> None:
        engine = MultiStrategyEngine()
        candles = _uptrend_all_pass_candles()
        signals = engine.evaluate("HDFCBANK", candles, strategy_names=["EMA_TREND"])
        assert len(signals) == 1
        assert signals[0].strategy_name == "EMA_TREND"
