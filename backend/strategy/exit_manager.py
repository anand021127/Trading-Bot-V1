"""Exit management for live and paper trading."""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple


class ExitManager:
    """Handles stop-loss and profit-target exit decisions."""

    def __init__(self, stop_loss_pct: float = 0.01, take_profit_pct: float = 0.02) -> None:
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct

    def should_exit(self, position: Dict[str, float], current_price: float) -> bool:
        """Return True when the position should be exited."""
        entry_price = position["entry_price"]
        side = position["side"]
        if side == "long":
            return current_price <= entry_price * (1 - self.stop_loss_pct) or current_price >= entry_price * (1 + self.take_profit_pct)
        if side == "short":
            return current_price >= entry_price * (1 + self.stop_loss_pct) or current_price <= entry_price * (1 - self.take_profit_pct)
        return False


class TrailingStopManager:
    """4-stage R-multiple trailing stop for open (long) positions —
    item #7. The stop only ever ratchets in the profitable direction; it
    never loosens back toward the original risk.

    Stages (tuned for option premium volatility / CE noise reduction):
        R < 0.7   → stage 0: original stop untouched
        R >= 0.7  → stage 1: move stop to breakeven (entry price)
        R >= 1.2  → stage 2: lock in 0.4R of profit
        R >= 1.8  → stage 3: lock in 0.9R of profit
        R >= 2.5  → stage 4: lock in 1.6R of profit, keeps trailing beyond
    """

    STAGE_THRESHOLDS: List[Tuple[float, float]] = [
        (0.7, 0.0),   # earlier move to breakeven (was 1.0) — critical for option premium noise
        (1.2, 0.4),   # lock 0.4R earlier
        (1.8, 0.9),   # lock 0.9R
        (2.5, 1.6),   # lock 1.6R, keeps trailing beyond
    ]

    def compute(
        self,
        entry_price: float,
        initial_stop: float,
        current_price: float,
        current_stop: Optional[float] = None,
    ) -> Dict[str, float]:
        """Returns {"stop": new_stop, "stage": int} — `stop` never goes
        below `current_stop` (or `initial_stop` if this is the first call)."""
        floor = current_stop if current_stop is not None else initial_stop
        risk = entry_price - initial_stop
        if risk <= 0:
            return {"stop": floor, "stage": 0}

        r_multiple = (current_price - entry_price) / risk
        stage = 0
        locked_r = None
        for i, (r_threshold, lock_r) in enumerate(self.STAGE_THRESHOLDS, start=1):
            if r_multiple >= r_threshold:
                stage = i
                locked_r = lock_r
        if stage >= 4:
            # Beyond stage 4, keep trailing ~1R behind current progress
            # (never below the stage-4 floor of 1.6R).
            locked_r = max(1.6, r_multiple - 1.0)

        if locked_r is None:
            return {"stop": floor, "stage": 0}

        candidate_stop = entry_price + locked_r * risk
        new_stop = max(floor, candidate_stop)
        return {"stop": round(new_stop, 2), "stage": stage}
