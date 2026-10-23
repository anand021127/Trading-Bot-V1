"""Capital vs V8-D's all-or-nothing lot sizing.

Finding (real backtest data): with the dashboard's ₹20,000 paper capital only 2 of 171 real V8-D
trades could be sized to one lot — every other valid setup is rejected with "0 lots", so the bot
cannot trade at all. Nothing told the operator. These tests pin the lower-bound maths to the
strategy's OWN sizing function and prove the explanation reaches the dashboard / Copilot."""
from __future__ import annotations

import random
from datetime import timedelta, timezone

from backend.copilot.full_context import _funding_mismatch
from backend.strategy.strategies.v8d_strategy import V8DStrategy
from backend.strategy.v8d_diagnostics import funding_check, min_equity_for_one_lot
from backend.tests.test_ai_decision_layer import APPROVE_JSON
from backend.tests.test_ai_pipeline_e2e import _engine, _pipeline
from backend.tests.test_scanner_runtime_pipeline import _scan, worker  # noqa: F401
from backend.tests.test_v8d_signal_to_execution import _find, _now_after_close, _scanner

IST = timezone(timedelta(hours=5, minutes=30))


def test_lower_bound_agrees_with_the_strategys_own_sizing_function():
    strat = V8DStrategy()
    rng = random.Random(3)
    for _ in range(400):
        prem, lot = rng.uniform(5, 400), rng.choice([20, 30, 35, 60, 65, 75, 120, 140])
        bound = min_equity_for_one_lot(prem, lot, strat)
        sl = prem * (1 - strat.stop_loss_pct)                 # the tightest stop V8-D can use
        below, _ = strat.calculate_position_size(bound * 0.999, prem, lot, sl)
        assert below == 0, (prem, lot, bound)                  # certain rejection below the bound
        at, _ = strat.calculate_position_size(bound * 1.001, prem, lot, sl)
        assert at >= 1, (prem, lot, bound)                     # and fundable just above it (tightest stop)


def test_real_backtest_scale_numbers():
    """NIFTY50 lot 65 @ ~₹66 premium (the backtest's real median) needs ≈ ₹49k; ₹20,000 cannot fund it."""
    strat = V8DStrategy()
    need = min_equity_for_one_lot(66.0, 65, strat)
    assert 45_000 < need < 60_000
    chain = {"ce": {"premium_ltp": 66.0, "lot_size": 65}, "pe": {"premium_ltp": 70.0, "lot_size": 65}}
    assert funding_check(chain, 20_000, strat)["can_fund_one_lot"] is False
    assert funding_check(chain, 100_000, strat)["can_fund_one_lot"] is True
    assert funding_check({}, 20_000, strat)["can_fund_one_lot"] is None          # unknown is reported as unknown


def test_valid_setup_at_20k_is_rejected_with_a_clear_explanation_and_fills_when_funded(worker):
    w, _ = _find("NIFTY50", lambda x: x["decision"] == "CE")
    worker.runtime.now_fn = lambda: _now_after_close(w).astimezone(IST)
    worker.db.save_setting("paper_reconcile_ok", "1")

    worker.runtime.realized_equity = 20_000.0               # the dashboard's configured capital
    eng = _engine(worker, [APPROVE_JSON])
    rec = _scan(worker, _scanner(worker, "NIFTY50", w, eng))
    assert rec["traded"] is False and rec["outcome"] == "SIGNAL_REJECTED", rec["reason"]
    assert any("0 lots" in r for r in rec["rejection"])
    assert rec["funding"]["can_fund_one_lot"] is False and rec["funding"]["equity"] == 20_000.0
    assert eng.provider.calls == []                          # V8-D rejected first: the AI is never consulted
    pipe, _ = _pipeline(worker)
    assert pipe["latest_signal"].startswith("SIGNAL REJECTED")
    assert "cannot fund ONE lot" in pipe["signal_detail"] and "TRADING_CAPITAL" in pipe["signal_detail"]
    need = rec["funding"]["min_equity_one_lot"]
    assert f"₹{need:,.0f}" in pipe["signal_detail"]

    msgs = _funding_mismatch(worker.db)                      # the dashboard/Copilot warning uses the same evidence
    assert msgs and msgs[0]["key"] == "capital_cannot_fund_one_lot" and "₹20,000" in msgs[0]["message"]
    assert "0 lots" in msgs[0]["message"]

    worker.runtime.realized_equity = 100_000.0               # with enough equity the SAME real setup fills
    rec2 = _scan(worker, _scanner(worker, "NIFTY50", w, _engine(worker, [APPROVE_JSON])))
    assert rec2["traded"] is True and rec2["funding"]["can_fund_one_lot"] is True
    assert _funding_mismatch(worker.db) == []                # warning clears once funded


def test_5000_equity_is_not_allowed_to_bypass_whole_lot_risk_caps():
    """Small-capital paper profile must report unaffordable whole lots, not force fills."""
    strat = V8DStrategy()
    chain = {
        "ce": {"premium_ltp": 66.0, "lot_size": 65},
        "pe": {"premium_ltp": 70.0, "lot_size": 65},
    }
    result = funding_check(chain, 5_000, strat)
    assert result["can_fund_one_lot"] is False
    assert result["min_equity_one_lot"] > 5_000
    quantity, sizing = strat.calculate_position_size(5_000, 66.0, 65, 66.0 * (1 - strat.stop_loss_pct))
    assert quantity == 0
    assert sizing["allowed_lots"] == 0


def test_risk_percentages_are_unchanged():
    s = V8DStrategy()
    assert (s.max_account_risk_pct, s.max_capital_alloc_pct, s.stop_loss_pct) == (0.025, 0.18, 0.28)
