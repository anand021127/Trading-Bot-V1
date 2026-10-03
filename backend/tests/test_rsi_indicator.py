"""RSI regression: result[-1] must be the RSI of the LAST close.

Bug fixed: the loop appended each RSI value BEFORE folding that bar's change into
the Wilder averages and never emitted the final update, so the last element was the
RSI of the PREVIOUS candle — V8-D (and the AI context) read an RSI one bar stale.
Verified here against the textbook Wilder RSI on REAL 5-minute index candles.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.indicators.rsi import calculate_rsi, rsi

ROOT = Path(__file__).resolve().parents[2]


def _textbook(closes, period=14):
    """Independent Wilder RSI (SMA seed, then (prev*(n-1)+x)/n). ref[k] = RSI of closes[period+k]."""
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    ag, al = sum(gains[:period]) / period, sum(losses[:period]) / period
    out = [100.0 if al == 0 else 100 - 100 / (1 + ag / al)]
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        out.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    return out


@pytest.mark.parametrize("symbol", ["NIFTY50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"])
def test_rsi_matches_textbook_wilder_on_real_candles(symbol):
    data = json.loads((ROOT / "real_data" / f"{symbol}_2024_5min.json").read_text())[:2500]
    closes = [float(c["close"]) for c in data]
    got, ref = calculate_rsi(closes, 14), _textbook(closes, 14)
    assert len(got) == len(closes) - 14 == len(ref)
    assert max(abs(a - b) for a, b in zip(got, ref)) < 1e-3          # 4-decimal rounding only
    assert abs(got[-1] - ref[-1]) < 1e-3                              # last value = RSI of the LAST close


def test_last_value_includes_the_last_candle_not_the_previous_one():
    data = json.loads((ROOT / "real_data" / "NIFTY50_2024_5min.json").read_text())[:500]
    closes = [float(c["close"]) for c in data]
    # the old behaviour was exactly rsi(closes[:-1])[-1]
    assert calculate_rsi(closes, 14)[-1] != calculate_rsi(closes[:-1], 14)[-1]
    assert calculate_rsi(closes, 14)[-2] == calculate_rsi(closes[:-1], 14)[-1]   # shifting by one bar lines up


def test_appending_a_candle_moves_the_last_rsi_in_the_right_direction():
    base = [100.0 + (i % 7) * 0.3 for i in range(60)]
    up, down = calculate_rsi(base + [base[-1] + 5.0], 14)[-1], calculate_rsi(base + [base[-1] - 5.0], 14)[-1]
    assert up > calculate_rsi(base, 14)[-1] > down            # the newest bar is reflected immediately


def test_edge_cases_unchanged():
    assert rsi([1.0] * 14, 14) == []                          # needs period+1 closes
    assert len(rsi([float(i) for i in range(15)], 14)) == 1   # exactly period+1 -> one value
    assert rsi([float(i) for i in range(30)], 14)[-1] == 100.0  # all gains
    assert rsi([float(30 - i) for i in range(30)], 14)[-1] == 0.0
