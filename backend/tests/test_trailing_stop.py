"""Unit tests for trailing_stop."""

from __future__ import annotations

import pytest

from backend.risk.trailing_stop import TrailingStop


def test_init_long() -> None:
    ts = TrailingStop(trailing_pct=0.05, side="long")
    stop = ts.init(entry_price=100.0)
    assert stop == 95.0


def test_update_moves_up_and_never_down() -> None:
    ts = TrailingStop(trailing_pct=0.05, side="long")
    ts.init(entry_price=100.0)
    s1 = ts.update(110.0)  # candidate = 104.5
    assert pytest.approx(s1, rel=1e-9) == 104.5
    s2 = ts.update(105.0)  # candidate = 99.75 -> stop should remain 104.5
    assert pytest.approx(s2, rel=1e-9) == 104.5


def test_short_side_moves_down() -> None:
    ts = TrailingStop(trailing_pct=0.02, side="short")
    stop0 = ts.init(entry_price=100.0)
    assert pytest.approx(stop0, rel=1e-9) == 102.0
    s1 = ts.update(95.0)  # candidate = 96.9 -> stop should move to 96.9
    assert pytest.approx(s1, rel=1e-9) == 96.9


def test_update_without_init_raises() -> None:
    ts = TrailingStop(trailing_pct=0.05, side="long")
    with pytest.raises(RuntimeError):
        ts.update(110.0)
