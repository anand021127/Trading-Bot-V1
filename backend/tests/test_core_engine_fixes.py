"""Tests for the 4 core fixes:
1. Expiry-day date evaluation against historical bar dates (not system today)
2. Cost model full statutory and exchange charge breakdown
3. Same-candle stop loss / target collision conservative resolution (CONSERVATIVE_STOP_FIRST)
"""
from datetime import date, datetime, timezone
import pytest

from backend.backtest.engine import BacktestEngine, CostConfig, BacktestTrade
from backend.strategy.strategies.option_premium import OptionPremiumStrategy


# =====================================================================
# Issue 1: Historical Expiry Day Date Evaluation
# =====================================================================

def test_expiry_day_evaluation_with_historical_bar():
    """Verify that expiry day is evaluated against the historical bar date, not date.today()."""
    strat = OptionPremiumStrategy()
    
    # 2024-08-29 was a Thursday monthly expiry for Nifty
    context = {"expiry_date": "2024-08-29", "current_bar_date": "2024-08-29"}
    assert strat._is_expiry_day(context) is True

    context_prior = {"expiry_date": "2024-08-29", "current_bar_date": "2024-08-28"}
    assert strat._is_expiry_day(context_prior) is False

    # Also test date extraction from candle timestamp
    candles_expiry_day = [
        {"timestamp": "2024-08-29T10:15:00+05:30", "open": 100, "high": 105, "low": 98, "close": 102, "volume": 1000}
    ]
    assert strat._is_expiry_day({"expiry_date": "2024-08-29"}, candles=candles_expiry_day) is True


def test_expiry_day_strategy_evaluation_gate():
    """Verify strategy evaluation blocks trade on historical expiry day when allow_expiry_day_entries=False."""
    strat = OptionPremiumStrategy(allow_expiry_day_entries=False)
    
    context = {
        "expiry_date": "2024-08-29",
        "current_bar_date": "2024-08-29",
        "underlying_trend": "BULLISH",
        "spot_price": 22000.0,
        "option_chain": [
            {
                "trading_symbol": "NIFTY24AUG22000CE",
                "strike": 22000.0,
                "option_type": "CE",
                "open_interest": 100000,
                "volume": 50000,
                "bid_ask_spread": 0.2,
                "last_price": 100.0,
                "theta": -5.0,
            }
        ],
    }
    
    # Build 25 candles
    candles = [
        {
            "timestamp": f"2024-08-29T09:{15 + i}:00+05:30",
            "open": 100.0 + i,
            "high": 102.0 + i,
            "low": 99.0 + i,
            "close": 101.5 + i,
            "volume": 5000,
        }
        for i in range(25)
    ]
    
    sig = strat.evaluate("NSE_FO|NIFTY24AUG22000CE", candles, context=context)
    assert sig.signal == "NONE"
    assert sig.conditions["not_expiry_day"] is False
    assert "NOT_EXPIRY_DAY FAILED" in sig.entry_reason


# =====================================================================
# Issue 2: Backtest Cost Model Comprehensive Breakdown
# =====================================================================

def test_cost_model_options_breakdown():
    """Verify complete statutory breakdown for an options trade."""
    cost_cfg = CostConfig()
    # Buy 50 Qty @ 100 (Buy Val = 5,000), Sell 50 Qty @ 150 (Sell Val = 7,500)
    res = cost_cfg.apply(entry=100.0, exit_price=150.0, qty=50, is_option=True)
    
    # Gross PnL: 7500 - 5000 = 2500
    assert res["gross_pnl"] == 2500.0
    
    # Brokerage: min(20, 5000 * 0.0005 = 2.50) + min(20, 7500 * 0.0005 = 3.75) = 6.25
    assert res["brokerage"] == 6.25
    
    # STT: 7500 * 0.0015 = 11.25 (sell side)
    assert res["stt"] == 11.25
    
    # Exchange turnover charges: (5000 + 7500) * 0.0005 = 6.25
    assert res["exchange_charges"] == 6.25
    
    # SEBI turnover charges: 12500 * 0.000001 = 0.0125
    assert res["sebi_charges"] == 0.0125
    
    # GST: 18% of (6.25 + 6.25 + 0.0125 = 12.5125) = 2.25
    assert res["gst"] == 2.25
    
    # Stamp duty: 5000 * 0.00003 = 0.15 (buy side)
    assert res["stamp_duty"] == 0.15
    
    # Slippage: 12500 * 0.0001 = 1.25
    assert res["slippage"] == 1.25
    
    # Total cost = 6.25 + 11.25 + 6.25 + 2.25 + 0.0125 + 0.15 + 1.25 = 27.4125 -> 27.41
    assert res["total_cost"] == 27.41
    assert res["net_pnl"] == round(2500.0 - 27.41, 2)


def test_cost_model_equity_breakdown():
    """Verify statutory breakdown for intraday equity trade."""
    cost_cfg = CostConfig()
    # Buy 100 Qty @ 1000 (100,000), Sell 100 Qty @ 1020 (102,000)
    res = cost_cfg.apply(entry=1000.0, exit_price=1020.0, qty=100, is_option=False)
    
    assert res["gross_pnl"] == 2000.0
    # Brokerage: min(20, 50) + min(20, 51) = 40.0
    assert res["brokerage"] == 40.0
    # STT: 102000 * 0.00025 = 25.5
    assert res["stt"] == 25.5
    assert res["total_cost"] > 0
    assert res["net_pnl"] == round(res["gross_pnl"] - res["total_cost"], 2)


# =====================================================================
# Issue 3: Same-Candle Collision Conservative Stop-First Resolution
# =====================================================================

def test_same_candle_exit_ambiguity_conservative_stop():
    """Verify that when a candle breaches BOTH Stop Loss and Target,
    CONSERVATIVE_STOP_FIRST triggers STOP_LOSS_HIT / TRAILING_STOP_HIT.
    """
    engine = BacktestEngine(capital=100000.0)
    
    position = {
        "symbol": "NSE_FO|54321",
        "strategy": "OPTION_PREMIUM",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "target": 120.0,
        "trailing_stop": 90.0,
        "quantity": 50,
        "confidence": 0.85,
    }
    
    # Ambiguous candle: Low goes to 85 (breaches stop 90), High goes to 125 (breaches target 120)
    ambiguous_bar = {
        "timestamp": "2024-08-29T10:30:00+05:30",
        "open": 98.0,
        "high": 125.0,
        "low": 85.0,
        "close": 115.0,
        "volume": 2000,
    }
    
    exit_reason = engine._check_exit(position, ambiguous_bar, window=[ambiguous_bar])
    assert exit_reason == "STOP_LOSS_HIT"
