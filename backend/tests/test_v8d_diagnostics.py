"""V8-D diagnostics: parity with the strategy over a year of REAL candles, and the
"valid setup -> BUY / invalid setup -> NO_SIGNAL / failed condition is identified"
guarantees. The diagnostics are NOT a second strategy: every value and the final
decision must equal the strategy's own output on every window tested."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.strategy.strategies.v8d_strategy import V8DStrategy
from backend.strategy.v8d_diagnostics import explain_chain, explain_pullback

ROOT = Path(__file__).resolve().parents[2]
SYMBOLS = ["NIFTY50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"]
WINDOW = 120
_CACHE = {}


def _load(sym):
    if sym not in _CACHE:
        _CACHE[sym] = json.loads((ROOT / "real_data" / f"{sym}_2024_5min.json").read_text())
    return _CACHE[sym]


def _windows(sym, step):
    c = _load(sym)
    for i in range(WINDOW, len(c), step):
        yield i, c[i - WINDOW + 1: i + 1]


@pytest.mark.parametrize("sym,step", [("NIFTY50", 1), ("BANKNIFTY", 3), ("FINNIFTY", 3),
                                      ("MIDCPNIFTY", 3), ("SENSEX", 3), ("BANKEX", 3)])
def test_diagnostics_equal_the_strategy_on_real_windows(sym, step):
    strat = V8DStrategy()
    n = setups = 0
    for _, w in _windows(sym, step):
        opt, _conds, ind = strat.detect_pullback_signal(w)
        e = explain_pullback(w, strat)
        assert e["decision"] == opt, (sym, w[-1]["timestamp"], e["decision"], opt)
        for k_diag, k_strat in (("ema20", "ema20"), ("ema50", "ema50"), ("rsi", "rsi")):
            assert e[k_diag] == pytest.approx(ind[k_strat], abs=1e-9)
        assert e["price"]["close"] == ind["close"]
        n += 1
        setups += opt is not None
    assert n > 1000
    assert setups > 0, f"no valid setup found on real {sym} data — the audit premise would be false"


def _first_window(sym, pred, step=1):
    strat = V8DStrategy()
    for i, w in _windows(sym, step):
        e = explain_pullback(w, strat)
        if pred(e):
            return i, w, e
    raise AssertionError("no matching real window found")


def _chain_for(strat, spot, sym, premium=85.0):
    atm = strat.get_atm_strike(spot, sym)
    return [
        {"strike": float(atm), "option_type": "CE", "instrument_key": f"NSE_FO|{sym}_CE_{atm}", "ltp": premium,
         "lot_size": 75, "freeze_quantity": 1800, "option_atr": 5.0, "atr": 5.0, "volume": 10000, "oi": 50000},
        {"strike": float(atm), "option_type": "PE", "instrument_key": f"NSE_FO|{sym}_PE_{atm}", "ltp": premium - 5,
         "lot_size": 75, "freeze_quantity": 1800, "option_atr": 5.0, "atr": 5.0, "volume": 8000, "oi": 40000},
    ]


@pytest.mark.parametrize("side", ["CE", "PE"])
def test_valid_real_setup_produces_a_buy_on_the_matching_side(side):
    strat = V8DStrategy()
    _, w, e = _first_window("NIFTY50", lambda x: x["decision"] == side)
    spot = w[-1]["close"]
    sig, log = strat.evaluate_v8d_signal(
        underlying_symbol="NIFTY50", underlying_candles=w, spot_price=spot,
        option_chain=_chain_for(strat, spot, "NIFTY50"), account_equity=100000.0, trades_today=0,
        kill_switch_active=False, reconciliation_ok=True)
    assert str(sig.signal) in ("BUY", "SignalType.BUY") or getattr(sig.signal, "value", "") == "BUY", \
        (sig.signal, sig.rejected_reasons)
    assert sig.indicators["selected_contract"]["option_type"] == side
    assert e["failed"] == [] and e["ce" if side == "CE" else "pe"]["all_pass"] is True


def test_invalid_real_setup_produces_no_signal_with_the_exact_failed_condition():
    strat = V8DStrategy()
    _, w, e = _first_window("NIFTY50", lambda x: x["decision"] is None and x["binding_condition"] == "trend")
    spot = w[-1]["close"]
    sig, log = strat.evaluate_v8d_signal(
        underlying_symbol="NIFTY50", underlying_candles=w, spot_price=spot,
        option_chain=_chain_for(strat, spot, "NIFTY50"), account_equity=100000.0, trades_today=0,
        kill_switch_active=False, reconciliation_ok=True)
    assert log.decision == "NO_SIGNAL" and "pullback/reversal criteria not met" in " ".join(sig.rejected_reasons)
    assert e["decision"] is None and e["binding_condition"] == "trend" and "trend" in e["failed"]
    assert "EMA20" in e["reason"] and "separation" in e["reason"]


@pytest.mark.parametrize("cond", ["trend", "pullback", "rsi", "reversal"])
def test_each_major_condition_is_identified_when_it_is_the_only_failure(cond):
    """On REAL windows (any of the six symbols, either side) where exactly one condition fails, the
    diagnostics name exactly that condition and the strategy independently returns no signal."""
    strat = V8DStrategy()
    hit = None
    for sym in SYMBOLS:
        for _i, w in _windows(sym, 1):
            e = explain_pullback(w, strat)
            if e["decision"] is None:
                for side in ("ce", "pe"):
                    if e[side]["failed"] == [cond]:
                        hit = (sym, side, w, e)
                        break
            if hit:
                break
        if hit:
            break
    assert hit, f"no real window where only {cond} fails"
    sym, side, w, e = hit
    opt, _c, _i = strat.detect_pullback_signal(w)
    assert opt is None
    assert e[side][cond]["pass"] is False and e[side][cond]["detail"]
    assert all(e[side][k]["pass"] for k in ("trend", "pullback", "rsi", "reversal") if k != cond)
    assert e["closest_side"] == side.upper() or e["binding_condition"] == cond


def test_insufficient_candles_reported_not_guessed():
    e = explain_pullback(_load("NIFTY50")[:20], V8DStrategy())
    assert e["evaluated"] is False and e["failed"] == ["sufficient_candles"] and "insufficient_candles" in e["reason"]
    assert e["decision"] is None


def test_all_requested_values_are_exposed():
    _, w, e = _first_window("NIFTY50", lambda x: x["evaluated"], step=7)
    for key in ("ema20", "ema50", "ema_separation_pct", "rsi", "atr14_underlying", "price", "pullback_band"):
        assert e[key] is not None, key
    for side in ("ce", "pe"):
        for cond in ("trend", "pullback", "rsi", "reversal"):
            assert "pass" in e[side][cond] and e[side][cond]["detail"]
    assert set(e["pullback_band"]) == {"ce", "pe"}
    assert "informational" in e["atr_note"]                    # ATR is honest: not an entry condition


def test_chain_diagnostics_report_availability_atm_and_premium():
    strat = V8DStrategy()
    ch = explain_chain(_chain_for(strat, 24012.0, "NIFTY50"), 24012.0, "NIFTY50", strat)
    assert ch["option_chain_count"] == 2 and ch["atm_strike"] == 24000
    assert ch["ce"]["premium_ltp"] == 85.0 and ch["pe"]["premium_ltp"] == 80.0 and ch["ce"]["instrument_key"]
    empty = explain_chain([], 24012.0, "NIFTY50", strat)
    assert empty["reason"] == "empty_option_chain" and empty["ce"] is None
    miss = explain_chain([c for c in _chain_for(strat, 24012.0, "NIFTY50") if c["option_type"] == "CE"], 24012.0, "NIFTY50", strat)
    assert miss["ce"] is not None and miss["pe"] is None       # a missing side is reported, not invented


def test_thresholds_in_the_diagnostics_still_match_the_strategy_source():
    """Drift guard: the literals the diagnostics mirror must still appear in the strategy."""
    src = (ROOT / "backend/strategy/strategies/v8d_strategy.py").read_text()
    for lit in ("0.0015", "1.003", "0.985", "45.0 <= curr_rsi <= 62.0", "0.997", "1.015",
                "38.0 <= curr_rsi <= 55.0", "0.55"):
        assert lit in src, lit
