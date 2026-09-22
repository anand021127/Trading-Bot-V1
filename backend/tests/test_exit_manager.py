"""Tests for TrailingStopManager — must match production STAGE_THRESHOLDS."""
from __future__ import annotations

from backend.strategy.exit_manager import TrailingStopManager


class TestTrailingStopManager:
    def setup_method(self) -> None:
        self.mgr = TrailingStopManager()
        self.entry = 100.0
        self.initial_stop = 95.0  # risk (R) = 5.0

    def test_stage_0_before_1r_keeps_original_stop(self) -> None:
        # Production: stage 0 until R >= 0.7
        result = self.mgr.compute(self.entry, self.initial_stop, current_price=102.0)  # 0.4R
        assert result["stage"] == 0
        assert result["stop"] == self.initial_stop

    def test_stage_1_at_1r_moves_to_breakeven(self) -> None:
        # Production stage 1 at R>=0.7 → stop at entry (lock 0.0R)
        result = self.mgr.compute(self.entry, self.initial_stop, current_price=105.0)  # 1.0R
        assert result["stage"] == 1
        assert result["stop"] == 100.0

    def test_stage_2_at_1_5r_locks_half_r(self) -> None:
        # Production stage 2 at R>=1.2 → lock 0.4R → stop = 100 + 0.4*5 = 102
        result = self.mgr.compute(self.entry, self.initial_stop, current_price=107.5)  # 1.5R
        assert result["stage"] == 2
        assert result["stop"] == 102.0

    def test_stage_3_at_2r_locks_full_r(self) -> None:
        # Production stage 3 at R>=1.8 → lock 0.9R → stop = 100 + 0.9*5 = 104.5
        result = self.mgr.compute(self.entry, self.initial_stop, current_price=110.0)  # 2R
        assert result["stage"] == 3
        assert result["stop"] == 104.5

    def test_stage_4_at_3r_locks_two_r_and_keeps_trailing(self) -> None:
        # Production stage 4 at R>=2.5 → lock 1.6R; at 3R still stage 4 stop 110
        result = self.mgr.compute(self.entry, self.initial_stop, current_price=115.0)  # 3R
        assert result["stage"] == 4
        assert result["stop"] == 110.0

        # Beyond stage 4, trails ~1R behind (r_multiple - 1)
        result2 = self.mgr.compute(self.entry, self.initial_stop, current_price=125.0)  # 5R
        assert result2["stage"] == 4
        assert result2["stop"] > result["stop"]

    def test_stop_never_loosens_when_price_pulls_back(self) -> None:
        first = self.mgr.compute(self.entry, self.initial_stop, current_price=110.0)
        assert first["stop"] == 104.5
        second = self.mgr.compute(
            self.entry, self.initial_stop, current_price=105.0, current_stop=first["stop"],
        )
        assert second["stop"] == 104.5

    def test_zero_or_negative_risk_returns_floor_safely(self) -> None:
        result = self.mgr.compute(entry_price=100.0, initial_stop=100.0, current_price=105.0)
        assert result["stage"] == 0
        assert result["stop"] == 100.0
