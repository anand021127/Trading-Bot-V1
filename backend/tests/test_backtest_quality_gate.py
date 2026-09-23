"""Coverage quality gate and V8-D identity on BacktestResult."""
from __future__ import annotations

from datetime import datetime, timedelta

from backend.backtest.engine import BacktestEngine


def _weekday_candles(start: str, days: int, bars_per_day: int = 2):
    """Sparse candles so coverage can be forced low or high."""
    d0 = datetime.fromisoformat(start)
    out = []
    made = 0
    day = d0
    while made < days:
        if day.weekday() < 5:
            for i in range(bars_per_day):
                ts = day.replace(hour=10, minute=i * 5)
                px = 24000 + made
                out.append({
                    "timestamp": ts.isoformat(),
                    "open": px, "high": px + 1, "low": px - 1, "close": px, "volume": 100,
                })
            made += 1
        day += timedelta(days=1)
    return out


def test_low_calendar_coverage_marks_invalid():
    engine = BacktestEngine(min_candles_required=1)
    candles = _weekday_candles("2024-10-01", days=2)  # 2 days of data
    res = engine.run(
        {"NIFTY50": candles},
        strategy_names=["V8_D_PULLBACK_ATM"],
        require_real_options=False,
        requested_start_date="2024-10-01",
        requested_end_date="2024-12-31",
        min_coverage_pct=80.0,
    )
    d = res.to_dict()
    assert d["strategy_names"] == ["V8_D_PULLBACK_ATM"]
    assert d["coverage_status"] == "FAILED_INCOMPLETE_COVERAGE"
    assert d["validity_status"] in ("INVALID", "INVALID_DATA")
    assert d["validity_reasons"]


def test_high_calendar_coverage_can_be_valid_without_option_lookups():
    engine = BacktestEngine(min_candles_required=1)
    candles = _weekday_candles("2024-10-01", days=5)
    res = engine.run(
        {"NIFTY50": candles},
        strategy_names=["V8_D_PULLBACK_ATM"],
        require_real_options=False,
        requested_start_date="2024-10-01",
        requested_end_date="2024-10-07",
        min_coverage_pct=50.0,
    )
    d = res.to_dict()
    assert "coverage_status" in d
    assert "validity_status" in d
    assert d["validity_status"] in ("VALID", "ZERO_TRADES", "INCONCLUSIVE", "INVALID_DATA", "INVALID", "UNKNOWN")
    assert d["strategy_names"] == ["V8_D_PULLBACK_ATM"]
    for tr in d.get("trade_log") or []:
        assert tr.get("strategy") != "OPTION_PREMIUM"
