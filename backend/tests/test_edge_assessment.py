"""Edge verdict for a backtest (fixed rules). Synthetic trade lists test the STATISTICS function."""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from backend.backtest.edge_assessment import MIN_TRADES, edge_assessment


def _trades(rs, risk=1000.0):
    """R-multiples -> trade dicts (entry 100, stop 90, qty = risk/10)."""
    return [{"entry_price": 100.0, "stop_loss": 90.0, "quantity": risk / 10, "net_pnl": r * risk, "gross_pnl": r * risk + 15}
            for r in rs]


def test_too_few_trades_is_not_a_conclusion():
    out = edge_assessment(_trades([1.0] * (MIN_TRADES - 1)))
    assert out["verdict"] == "INSUFFICIENT_DATA" and "at least" in out["message"]


def test_clearly_positive_negative_and_noise_are_told_apart():
    rng = random.Random(1)
    pos = edge_assessment(_trades([rng.gauss(0.40, 1.0) for _ in range(400)]))
    neg = edge_assessment(_trades([rng.gauss(-0.40, 1.0) for _ in range(400)]))
    zero = edge_assessment(_trades([rng.gauss(0.0, 1.0) for _ in range(400)]))
    assert pos["verdict"] == "POSITIVE_EXPECTANCY" and pos["ci95_low"] > 0
    assert neg["verdict"] == "NEGATIVE_EXPECTANCY" and neg["ci95_high"] < 0 and "Do not trade" in neg["message"]
    assert zero["verdict"] == "NO_DEMONSTRATED_EDGE" and zero["ci95_low"] < 0 < zero["ci95_high"]


def test_deterministic_and_reports_gross_and_net():
    rs = [(-1.0 if i % 3 else 1.5) for i in range(90)]            # 30 wins of 1.5R, 60 losses of 1R
    a, b = edge_assessment(_trades(rs)), edge_assessment(_trades(rs))
    assert a == b                                                  # fixed seed -> reproducible
    assert a["mean_r"] == pytest.approx((30 * 1.5 - 60) / 90, abs=1e-3)
    assert a["gross_mean_r"] > a["mean_r"]                         # costs reduce the result
    assert "independent" in a["caveats"]


def test_bad_rows_are_ignored_not_fatal():
    t = _trades([0.5] * 40) + [{"entry_price": 100, "stop_loss": 120, "quantity": 5, "net_pnl": 1},   # risk <= 0
                                {"net_pnl": 5}, {}]
    assert edge_assessment(t)["n_trades"] == 40


def test_real_trade_log_if_present_is_judged_negative():
    """The operator's real 171-trade V8-D backtest (when the export is available) is a significant LOSER."""
    import csv
    import glob
    import io
    paths = glob.glob("/mnt/user-data/uploads/upstox_backtest_*.csv")
    if not paths:
        pytest.skip("real backtest export not available in this environment")
    raw = Path(paths[0]).read_text(encoding="utf-8").replace("\r\n", "\n")
    rows = list(csv.DictReader(io.StringIO(raw.split("=== TRADE LOG ===")[1].strip())))
    out = edge_assessment(rows)
    assert out["verdict"] == "NEGATIVE_EXPECTANCY" and out["n_trades"] == 171 and out["ci95_high"] < 0


def test_copilot_backtest_summary_carries_the_verdict():
    import inspect
    from backend.copilot import full_context
    src = inspect.getsource(full_context._backtest_summary)
    assert "edge_assessment" in src and 'summary["edge"]' in src
