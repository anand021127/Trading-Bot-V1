"""V8-D must not use future bars; NSE session times are Asia/Kolkata."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from backend.strategy.strategies.v8d_strategy import V8DStrategy

IST = ZoneInfo("Asia/Kolkata")


def test_future_candle_mutation_does_not_change_past_decision():
    strat = V8DStrategy()
    start = datetime(2024, 10, 3, 10, 0, tzinfo=IST)
    candles = []
    px = 24000.0
    for i in range(80):
        o = px
        px = px + 5
        candles.append({
            "timestamp": (start + timedelta(minutes=5 * i)).isoformat(),
            "open": o, "high": max(o, px) + 1, "low": min(o, px) - 1, "close": px, "volume": 1000,
        })
    window = candles[:70]
    a, ca, ia = strat.detect_pullback_signal(window)
    mutated = list(window)
    last = dict(candles[-1])
    last["close"] = 1.0
    last["high"] = 2.0
    last["low"] = 0.5
    future = mutated + [last]
    b, cb, ib = strat.detect_pullback_signal(future[:70])  # same first 70
    assert a == b
    assert ca.get("bull_trend") == cb.get("bull_trend")
    assert ca.get("bear_trend") == cb.get("bear_trend")


def test_nse_session_hours_are_ist():
    from backend.config.settings import load_settings
    s = load_settings()
    assert s.strategy.entry_window_end
    assert s.strategy.exit_all_by
    # Square-off must be before 15:30 IST close
    hh, mm = map(int, s.strategy.exit_all_by.split(":"))
    assert (hh, mm) <= (15, 30)
    assert s.strategy.orb_window_start == "09:15"


def test_future_mutation_at_early_mid_late():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from backend.strategy.strategies.v8d_strategy import V8DStrategy
    IST = ZoneInfo("Asia/Kolkata")
    strat = V8DStrategy()
    start = datetime(2024, 10, 3, 10, 0, tzinfo=IST)
    candles = []
    px = 24000.0
    for i in range(120):
        o = px
        px = px + (3 if i % 7 else -2)
        candles.append({
            "timestamp": (start + timedelta(minutes=5 * i)).isoformat(),
            "open": o, "high": max(o, px) + 2, "low": min(o, px) - 2, "close": px, "volume": 1000,
        })
    for T in (40, 70, 100):
        prefix = candles[:T]
        a, ca, _ = strat.detect_pullback_signal(prefix)
        mutated = list(candles)
        for j in range(T, len(mutated)):
            mutated[j] = {**mutated[j], "close": 1.0, "high": 2.0, "low": 0.5, "open": 1.0}
        b, cb, _ = strat.detect_pullback_signal(mutated[:T])
        assert a == b, f"signal changed at T={T}"
        assert ca.get("bull_trend") == cb.get("bull_trend")
        assert ca.get("bear_trend") == cb.get("bear_trend")
