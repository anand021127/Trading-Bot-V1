"""Mechanics of the strategy research harness (scripts/strategy_research.py).

The synthetic candles here test the SIMULATOR's arithmetic (stop/target/gap/ambiguity/time-exit/cost),
not market behaviour; the research results themselves use only real candles."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("strategy_research", ROOT / "scripts" / "strategy_research.py")
sr = importlib.util.module_from_spec(_spec)
sys.modules["strategy_research"] = sr
_spec.loader.exec_module(sr)


def _day(prices, date="2024-03-04"):
    out = []
    for k, (o, h, l, c) in enumerate(prices):
        m = 9 * 60 + 15 + 5 * k
        out.append({"timestamp": f"{date}T{m // 60:02d}:{m % 60:02d}:00+05:30", "open": o, "high": h, "low": l, "close": c, "volume": 0})
    return out


def _flat(n=40, px=100.0, rng=0.5):
    return [(px, px + rng, px - rng, px)] * n


def _series_with(tail):
    return sr.Series(_day(_flat(40) + tail + _flat(10)))


def test_target_stop_gap_ambiguity_and_time_exit_values():
    atr = 1.0
    risk = sr.STOP_ATR * atr
    s = _series_with([(100, 100 + sr.RR * risk + 0.1, 99.9, 100)]); s.atr[:] = atr
    assert sr.simulate(s, 39, +1)[0] == pytest.approx(sr.RR)                       # target
    s = _series_with([(100, 100.2, 100 - risk - 0.1, 100)]); s.atr[:] = atr
    assert sr.simulate(s, 39, +1)[0] == pytest.approx(-1.0)                        # stop
    s = _series_with([(100, 100 + risk + 0.1, 99.9, 100)]); s.atr[:] = atr
    assert sr.simulate(s, 39, -1)[0] == pytest.approx(-1.0)                        # short side mirrors
    s = _series_with([(100, 100 + sr.RR * risk + 0.1, 100 - risk - 0.1, 100)]); s.atr[:] = atr
    assert sr.simulate(s, 39, +1)[0] == pytest.approx((sr.RR - 1) / 2)             # both levels touched: neutral expectation
    gap_dn = [(100, 100.2, 99.9, 100), (100 - 2 * risk,) * 4]
    s = sr.Series(_day(_flat(40) + gap_dn + _flat(10))); s.atr[:] = atr
    assert sr.simulate(s, 39, +1)[0] == pytest.approx(-2.0)                        # gap THROUGH the stop: worse than -1R
    gap_up = [(100, 100.2, 99.9, 100), (100 + 3 * risk,) * 4]
    s = sr.Series(_day(_flat(40) + gap_up + _flat(10))); s.atr[:] = atr
    assert sr.simulate(s, 39, +1)[0] == pytest.approx(3.0)                         # gap through the target: credited symmetrically
    s = sr.Series(_day(_flat(80))); s.atr[:] = atr
    r, jx = sr.simulate(s, 39, +1)
    assert r == pytest.approx(0.0) and jx > 40                                     # nothing happens -> time exit ~0R


def test_signal_is_taken_at_the_next_open_never_the_signal_bar():
    """No look-ahead: a spike in the SIGNAL bar's own high/low must not influence the trade."""
    s = sr.Series(_day(_flat(39) + [(100, 150, 50, 100)] + _flat(30))); s.atr[:] = 1.0
    assert sr.simulate(s, 39, +1)[0] == pytest.approx(0.0)


def test_cost_is_charged_in_r_and_scales_with_price_over_risk():
    s = sr.Series(_day(_flat(80))); s.atr[:] = 1.0
    t = sr.run(s, {45: 1})[0]
    assert t["R_gross"] - t["R_net"] == pytest.approx((sr.COST_BP / 1e4) * s.c[45] / (sr.STOP_ATR * 1.0))


def test_one_position_at_a_time():
    s = sr.Series(_day(_flat(80))); s.atr[:] = 1.0
    assert len(sr.run(s, {41: 1, 42: 1, 43: -1})) == 1


def test_cluster_bootstrap_resamples_days_and_is_deterministic():
    tr = [{"symbol": "A", "date": f"d{i // 3}", "R_net": (1.0 if i % 2 else -0.5)} for i in range(60)]
    a, b = sr.cluster_bootstrap(tr, "R_net"), sr.cluster_bootstrap(tr, "R_net")
    assert a == b and a["lo95"] < a["mean"] < a["hi95"] and a["n"] == 60


def test_decision_rule_and_documented_limits():
    assert "Bonferroni" in sr.__doc__ and "hold-out" in sr.__doc__ and "NOT simulated" in sr.__doc__
    assert (sr.STOP_ATR, sr.RR, sr.COST_BP) == (1.5, 1.5, 1.5)


def test_published_results_reflect_the_decision_rule():
    f = ROOT / "analysis" / "strategy_research_results.json"
    if not f.exists():
        pytest.skip("run scripts/strategy_research.py to generate the results")
    res = json.loads(f.read_text())["results"]
    assert abs(np.mean(res["_control_seed_gross_means"])) < 0.05                   # the simulator is unbiased
    for name, r in res.items():
        if name.startswith("_") or name == "CONTROL_RANDOM":
            continue
        qual = (r["net"]["lo99_4"] > 0 and r["H1"]["mean"] > 0 and r["H2"]["mean"] > 0
                and r["symbols_positive"] >= 4 and r["holdout"]["mean"] > 0)
        assert r["qualifies"] == qual                                              # stored verdicts follow the pre-registered rule
