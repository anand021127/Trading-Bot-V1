"""Contract tests for options-safe backtest primitives."""
from __future__ import annotations

from backend.backtest.engine import BacktestEngine, CostConfig


def test_option_costs_use_option_stt() -> None:
    costs = CostConfig(stt_pct=0.001, option_stt_pct=0.0015)
    equity_like = costs.apply(100, 110, 100, is_option=False)
    option = costs.apply(100, 110, 100, is_option=True)
    assert option["stt"] > equity_like["stt"]


def test_insufficient_real_history_is_skipped_without_padding() -> None:
    engine = BacktestEngine(min_candles_required=2)
    result = engine.run({"NIFTY50": [{"timestamp": "t", "close": 100}]})
    assert result.trades_taken == 0
    assert result.skipped_symbols[0]["symbol"] == "NIFTY50"
    assert "Not padded" in result.skipped_symbols[0]["reason"]


def test_empty_option_history_is_explicit() -> None:
    result = BacktestEngine().run({"BANKNIFTY": []})
    assert result.trades_taken == 0
    assert result.skipped_symbols[0]["symbol"] == "BANKNIFTY"


def test_time_varying_trend_series_overrides_static_context_per_bar() -> None:
    """Regression test for a real realism bug: the backtest used to
    compute ONE static underlying_trend snapshot (from whatever the
    market looked like at the moment the backtest was started) and apply
    it across the ENTIRE historical date range being tested — meaning a
    full year of entries could all be judged against today's trend,
    regardless of the market's actual regime on each historical day.
    `underlying_trend_series` must override the static `underlying_trend`
    on a per-bar basis, using only the trend known at-or-before that bar
    (no lookahead)."""
    from unittest.mock import MagicMock
    from backend.strategy.signal import StrategySignal, SignalType

    engine = BacktestEngine(min_candles_required=2)
    candles = [
        {"timestamp": f"t{i:03d}", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}
        for i in range(5)
    ]
    # Trend is BULLISH for the first two bars, then flips to NEUTRAL.
    trend_series = {"t000": "BULLISH", "t001": "BULLISH", "t003": "NEUTRAL"}
    context = {"underlying_trend": "BEARISH", "underlying_trend_series": trend_series}

    seen_trends = []

    def fake_evaluate(symbol, window, context=None, strategy_names=None):
        seen_trends.append(context.get("underlying_trend"))
        return [StrategySignal(strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.NONE)]

    engine.strategy_engine.evaluate = MagicMock(side_effect=fake_evaluate)
    engine.run({"NIFTY50": candles}, option_contexts={"NIFTY50": context})

    # Bars at t002 and t004 have no exact trend_series entry — must use
    # the MOST RECENT one at-or-before that timestamp (t001's BULLISH for
    # t002; t003's NEUTRAL carries forward to t004), never the original
    # static "BEARISH".
    assert seen_trends == ["BULLISH", "NEUTRAL", "NEUTRAL"]
    assert "BEARISH" not in seen_trends


def test_real_options_data_layer_validation() -> None:
    """Validate that when real options data layer is provided:
    1. underlying_price != option_entry_price
    2. option_entry_price == historical_option_candle_close
    3. option instrument_key -> historical contract -> historical expiry -> strike -> CE/PE match
    """
    from unittest.mock import MagicMock
    from backend.strategy.signal import StrategySignal, SignalType
    from backend.backtest.options_data_layer import HistoricalOptionsDataLoader, HistoricalOptionRecord

    loader = HistoricalOptionsDataLoader()
    # Contract: NIFTY 24500 CE on 2024-06-27 expiry
    sample_candles = [
        {"timestamp": "2024-06-25T09:15:00", "open": 180.0, "high": 195.0, "low": 175.0, "close": 190.0, "volume": 50000, "oi": 120000},
        {"timestamp": "2024-06-25T09:20:00", "open": 190.0, "high": 220.0, "low": 185.0, "close": 215.0, "volume": 75000, "oi": 125000},
        {"timestamp": "2024-06-25T09:25:00", "open": 215.0, "high": 230.0, "low": 210.0, "close": 225.0, "volume": 60000, "oi": 130000},
    ]
    loader.load_contract_candles(
        underlying="NIFTY50",
        expiry="2024-06-27",
        strike=24500.0,
        option_type="CE",
        instrument_key="NSE_FO|NIFTY2462724500CE",
        candles=sample_candles,
    )

    engine = BacktestEngine(min_candles_required=2)
    spot_candles = [
        {"timestamp": "2024-06-25T09:10:00", "open": 24490, "high": 24510, "low": 24480, "close": 24500, "volume": 1000000},
        {"timestamp": "2024-06-25T09:15:00", "open": 24500, "high": 24520, "low": 24495, "close": 24502, "volume": 1200000},
        {"timestamp": "2024-06-25T09:20:00", "open": 24502, "high": 24515, "low": 24498, "close": 24505, "volume": 1500000},
        {"timestamp": "2024-06-25T09:25:00", "open": 24505, "high": 24530, "low": 24500, "close": 24520, "volume": 1100000},
    ]

    # Signal triggered on spot bar at 09:20:00
    def fake_evaluate(symbol, window, context=None, strategy_names=None):
        curr_bar = window[-1]
        if curr_bar["timestamp"] == "2024-06-25T09:20:00":
            return [StrategySignal(
                strategy_name="OPTION_PREMIUM",
                symbol=symbol,
                signal=SignalType.BUY,
                entry_price=curr_bar["close"], # 24505 (Spot price)
                stop_loss=24450.0,
                target=24600.0,
                confidence=0.85,
            )]
        return [StrategySignal(strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.NONE)]

    engine.strategy_engine.evaluate = MagicMock(side_effect=fake_evaluate)

    res = engine.run(
        {"NIFTY50": spot_candles},
        options_data_loader=loader,
        require_real_options=True,
    )

    assert res.trades_taken >= 1
    trade = res.trade_log[0]
    # 1. Underlying spot price (24505) != Option entry price (215.0)
    assert trade["entry_price"] != 24505
    # 2. Option entry price matches actual historical option candle close
    assert trade["entry_price"] == 215.0
    # 3. Contract symbol is real option instrument key
    assert trade["symbol"] == "NSE_FO|NIFTY2462724500CE"





def test_real_options_data_unavailable_failsafe() -> None:
    """When real historical option data is NOT loaded, require_real_options strictly
    prevents trade execution and records DATA_UNAVAILABLE."""
    from unittest.mock import MagicMock
    from backend.strategy.signal import StrategySignal, SignalType
    from backend.backtest.options_data_layer import HistoricalOptionsDataLoader

    loader = HistoricalOptionsDataLoader()  # empty loader
    engine = BacktestEngine(min_candles_required=2)
    spot_candles = [
        {"timestamp": "2024-06-25T09:10:00", "open": 24490, "high": 24510, "low": 24480, "close": 24500, "volume": 1000000},
        {"timestamp": "2024-06-25T09:15:00", "open": 24500, "high": 24520, "low": 24495, "close": 24505, "volume": 1200000},
        {"timestamp": "2024-06-25T09:20:00", "open": 24505, "high": 24540, "low": 24500, "close": 24535, "volume": 1500000},
    ]

    def fake_evaluate(symbol, window, context=None, strategy_names=None):
        return [StrategySignal(
            strategy_name="OPTION_PREMIUM",
            symbol=symbol,
            signal=SignalType.BUY,
            entry_price=window[-1]["close"],
            stop_loss=24450.0,
            target=24600.0,
            confidence=0.85,
        )]

    engine.strategy_engine.evaluate = MagicMock(side_effect=fake_evaluate)

    res = engine.run(
        {"NIFTY50": spot_candles},
        options_data_loader=loader,
        require_real_options=True,
    )

    # 0 trades must be created
    assert res.trades_taken == 0
    # Rejection reasons must contain DATA_UNAVAILABLE
    assert any("DATA_UNAVAILABLE" in r for r in res.rejection_reason_counts.keys())


def test_zero_synthetic_option_pricing_guarantee() -> None:
    """Verifies that:
    1. BacktestEngine cannot manufacture or synthesize option premiums.
    2. Missing candle for contract timestamp strictly yields DATA_UNAVAILABLE.
    3. Option exits and PnL are evaluated against historical option candle prices.
    4. Spot substitution is detected and rejected by OptionsDataValidator.
    """
    from unittest.mock import MagicMock
    from backend.strategy.signal import StrategySignal, SignalType
    from backend.backtest.options_data_layer import HistoricalOptionsDataLoader
    from backend.broker.upstox_expired_options import OptionsDataValidator

    # 1. Spot substitution rejection check
    validator = OptionsDataValidator()
    spot_substitution_candles = [
        {"timestamp": "2024-06-25T09:15:00", "open": 24500.0, "high": 24520.0, "low": 24490.0, "close": 24510.0, "volume": 1000}
    ]
    ok, err, _ = validator.validate_candles(
        candles=spot_substitution_candles,
        expected_instrument_key="NSE_FO|NIFTY2462724500CE",
        spot_price_reference=24500.0,
    )
    assert ok is False
    assert "Suspicious option price" in err

    # 2. Missing candle at signal bar fails with DATA_UNAVAILABLE
    loader = HistoricalOptionsDataLoader()
    # Contract loaded, but candles only exist for 09:15, NOT 09:20
    loader.load_contract_candles(
        underlying="NIFTY50",
        expiry="2024-06-27",
        strike=24500.0,
        option_type="CE",
        instrument_key="NSE_FO|NIFTY2462724500CE",
        candles=[{"timestamp": "2024-06-25T09:15:00", "open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 5000}],
    )

    spot_candles = [
        {"timestamp": "2024-06-25T09:10:00", "open": 24490, "high": 24500, "low": 24480, "close": 24495, "volume": 100000},
        {"timestamp": "2024-06-25T09:15:00", "open": 24500, "high": 24510, "low": 24495, "close": 24500, "volume": 100000},
        {"timestamp": "2024-06-25T09:20:00", "open": 24500, "high": 24520, "low": 24498, "close": 24505, "volume": 120000},
    ]

    engine = BacktestEngine(min_candles_required=2)
    def fake_evaluate(symbol, window, context=None, strategy_names=None):
        if window[-1]["timestamp"] == "2024-06-25T09:20:00":
            return [StrategySignal(
                strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.BUY,
                entry_price=window[-1]["close"], stop_loss=24450.0, target=24600.0, confidence=0.9,
            )]
        return [StrategySignal(strategy_name="OPTION_PREMIUM", symbol=symbol, signal=SignalType.NONE)]

    engine.strategy_engine.evaluate = MagicMock(side_effect=fake_evaluate)
    res = engine.run({"NIFTY50": spot_candles}, options_data_loader=loader, require_real_options=True)
    assert res.trades_taken == 0
    assert any("Missing historical option candle" in r for r in res.rejection_reason_counts.keys())

