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
    """4-stage ATR/R-multiple trailing stop for open (long) positions —
    item #7. The stop only ever ratchets in the profitable direction; it
    never loosens back toward the original risk.

    Stages, based on how many "R" (initial risk = entry - initial_stop)
    the position has moved in its favor:
        R < 1.0   → stage 0: original stop untouched
        R >= 1.0  → stage 1: move stop to breakeven (entry price)
        R >= 1.5  → stage 2: lock in 0.5R of profit
        R >= 2.0  → stage 3: lock in 1.0R of profit (this is usually `target`)
        R >= 3.0  → stage 4: lock in 2.0R of profit, keeps trailing beyond
    """

    STAGE_THRESHOLDS: List[Tuple[float, float]] = [
        (1.0, 0.0),   # move to breakeven
        (1.5, 0.5),   # lock 0.5R
        (2.0, 1.0),   # lock 1.0R
        (3.0, 2.0),   # lock 2.0R, keeps trailing at this ratio beyond stage 4
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
            # Beyond stage 4, keep trailing at 2R behind current price's
            # own R-multiple progress (never below the stage-4 floor).
            locked_r = max(2.0, r_multiple - 1.0)

        if locked_r is None:
            return {"stop": floor, "stage": 0}

        candidate_stop = entry_price + locked_r * risk
        new_stop = max(floor, candidate_stop)
        return {"stop": round(new_stop, 2), "stage": stage}
