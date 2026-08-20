"""Professional risk management for live algorithmic trading.

Enforces all safety rules:
- Max daily loss
- Max trades per day
- Max consecutive losses
- Max concurrent positions
- Emergency kill switch
- Cooldown after losing streak
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class RiskManager:
    """Production-grade risk manager. All safety rules in one place."""

    capital: float
    daily_loss_limit: float = 0.02        # 2% of capital
    max_trades_per_day: int = 4
    max_concurrent_positions: int = 2
    max_consecutive_losses: int = 3
    max_position_exposure: float = 0.60    # 60% of capital — max notional across all positions
    pause_minutes_after_losses: int = 30

    # Daily state (reset each morning)
    pnl_today: float = 0.0
    trades_today: int = 0
    consecutive_losses: int = 0
    day: date = field(default_factory=date.today)

    # Control state
    kill_switch_active: bool = False
    paused_until: Optional[datetime] = None
    open_positions: int = 0

    # ─── Core method (used by diagnostics) ───────────────────────────────────

    def can_take_trade(self, symbol: str = "") -> Tuple[bool, str]:
        """
        Primary entry point: return (allowed, reason).
        Returns (True, "") if trade can proceed, (False, reason) if blocked.
        """
        self._check_new_day()

        if self.kill_switch_active:
            return False, "Emergency kill switch is ACTIVE. Bot stopped."

        if self._is_paused():
            resume = self.paused_until.strftime("%H:%M") if self.paused_until else "unknown"
            return False, f"Bot is paused after {self.max_consecutive_losses} consecutive losses. Resumes at {resume}."

        if self.pnl_today <= -self.daily_loss_amount():
            return False, f"Daily loss limit hit: ₹{abs(self.pnl_today):.0f} / ₹{self.daily_loss_amount():.0f}"

        if self.trades_today >= self.max_trades_per_day:
            return False, f"Max trades per day reached: {self.trades_today}/{self.max_trades_per_day}"

        if self.open_positions >= self.max_concurrent_positions:
            return False, f"Max concurrent positions: {self.open_positions}/{self.max_concurrent_positions}"

        return True, ""

    # ─── Backward-compat alias ────────────────────────────────────────────────

    def can_open_trade(self, potential_loss: float) -> bool:
        """Legacy method — check if a trade with this potential loss is allowed."""
        if potential_loss < 0:
            raise ValueError("potential_loss must be non-negative")
        allowed, _ = self.can_take_trade()
        if not allowed:
            return False
        projected = self.pnl_today - potential_loss
        return projected >= -self.daily_loss_amount()

    # ─── State management ─────────────────────────────────────────────────────

    def record_trade_opened(self) -> None:
        """Call when a new trade is opened."""
        self.trades_today += 1
        self.open_positions += 1

    def record_trade_result(self, pnl: float) -> None:
        """Call when a trade is closed with its realized P&L."""
        self.pnl_today += pnl
        self.open_positions = max(0, self.open_positions - 1)

        if pnl < 0:
            self.consecutive_losses += 1
            logger.warning("Loss recorded. Consecutive losses: %d", self.consecutive_losses)
            if self.consecutive_losses >= self.max_consecutive_losses:
                self.paused_until = datetime.now() + timedelta(minutes=self.pause_minutes_after_losses)
                logger.warning(
                    "Max consecutive losses (%d) reached. Bot paused until %s.",
                    self.consecutive_losses,
                    self.paused_until.strftime("%H:%M"),
                )
        else:
            self.consecutive_losses = 0

        if self.pnl_today <= -self.daily_loss_amount():
            logger.error(
                "DAILY LOSS LIMIT HIT: ₹%.2f. Bot stopping for today.", abs(self.pnl_today)
            )

    def activate_kill_switch(self, reason: str = "Manual") -> None:
        """Emergency stop — prevents ALL new trades immediately."""
        self.kill_switch_active = True
        logger.critical("KILL SWITCH ACTIVATED: %s", reason)

    def deactivate_kill_switch(self) -> None:
        """Resume trading after emergency stop."""
        self.kill_switch_active = False
        logger.info("Kill switch deactivated. Trading resumed.")

    def reset_for_new_day(self, new_day: Optional[date] = None) -> None:
        """Reset all daily counters. Call at market open each day."""
        self.day = new_day or date.today()
        self.pnl_today = 0.0
        self.trades_today = 0
        self.consecutive_losses = 0
        self.paused_until = None
        self.open_positions = 0
        logger.info("Risk manager reset for %s", self.day)

    # ─── Lot-level risk check (V21-FINAL Item 10) ──────────────────────────

    def check_lot_risk(
        self,
        entry_price: float,
        stop_loss: float,
        lot_size: int,
    ) -> Tuple[bool, str]:
        """Reject if the risk of even ONE lot exceeds the allowed per-trade risk.

        This prevents the pathological case where risk-based sizing says
        'trade 0 units' but lot rounding forces qty=lot_size anyway, creating
        a position that violates the configured risk limit.

        Returns (allowed, reason).
        """
        allowed_risk = self.capital * (self.daily_loss_limit / self.max_trades_per_day
                                        if self.max_trades_per_day > 0 else 0.01)
        # Also use the position-sizer's risk_per_trade if we can
        # but capital * a reasonable per-trade fraction is the baseline
        risk_per_lot = abs(entry_price - stop_loss) * lot_size
        if risk_per_lot > allowed_risk:
            return False, (
                f"Minimum lot risk ₹{risk_per_lot:.0f} (|{entry_price:.2f} - {stop_loss:.2f}| "
                f"× {lot_size}) exceeds maximum allowed trade risk ₹{allowed_risk:.0f}. "
                f"Trade rejected to protect capital."
            )
        return True, ""

    # ─── Position exposure check (V21-FINAL Item 11) ─────────────────────

    # Running tally of current notional exposure, updated by the engine
    current_exposure: float = 0.0

    def check_exposure(
        self,
        new_entry_price: float,
        new_quantity: int,
    ) -> Tuple[bool, str]:
        """Reject if adding this position would exceed max_position_exposure.

        Returns (allowed, reason).
        """
        new_notional = new_entry_price * new_quantity
        max_allowed = self.capital * self.max_position_exposure
        projected = self.current_exposure + new_notional
        if projected > max_allowed:
            return False, (
                f"Position exposure ₹{projected:.0f} would exceed maximum "
                f"₹{max_allowed:.0f} ({self.max_position_exposure*100:.0f}% of "
                f"₹{self.capital:.0f} capital). Current exposure: ₹{self.current_exposure:.0f}, "
                f"new trade: ₹{new_notional:.0f}."
            )
        return True, ""

    def record_exposure_opened(self, notional: float) -> None:
        """Track exposure when a position is opened."""
        self.current_exposure += notional

    def record_exposure_closed(self, notional: float) -> None:
        """Track exposure when a position is closed."""
        self.current_exposure = max(0.0, self.current_exposure - notional)

    # ─── Computed properties ──────────────────────────────────────────────────

    def daily_loss_amount(self) -> float:
        """Max allowed loss in rupees today."""
        return self.capital * self.daily_loss_limit

    def remaining_loss_capacity(self) -> float:
        """Remaining allowed loss before daily limit is hit."""
        return max(0.0, self.daily_loss_amount() + self.pnl_today)

    def daily_loss_used_pct(self) -> float:
        """Percentage of daily loss limit consumed (0-100)."""
        if self.daily_loss_amount() == 0:
            return 0.0
        used = -min(self.pnl_today, 0)
        return min((used / self.daily_loss_amount()) * 100, 100.0)

    def get_status(self) -> dict:
        """Return current risk status dict for dashboard."""
        self._check_new_day()
        allowed, reason = self.can_take_trade()
        status = "ACTIVE"
        if self.kill_switch_active:
            status = "STOPPED"
        elif self._is_paused():
            status = "PAUSED"
        elif not allowed:
            status = "STOPPED"

        max_exposure = self.capital * self.max_position_exposure

        return {
            "is_trading_allowed": allowed,
            "status": status,
            "stop_reason": reason or None,
            "consecutive_losses": self.consecutive_losses,
            "daily_loss_used_pct": round(self.daily_loss_used_pct(), 1),
            "trades_used": self.trades_today,
            "max_trades": self.max_trades_per_day,
            "daily_pnl": round(self.pnl_today, 2),
            "open_positions": self.open_positions,
            "kill_switch_active": self.kill_switch_active,
            # V21-FINAL: exposure tracking for dashboard
            "capital": round(self.capital, 2),
            "current_exposure": round(self.current_exposure, 2),
            "available_exposure": round(max(0, max_exposure - self.current_exposure), 2),
            "max_exposure": round(max_exposure, 2),
            "max_exposure_pct": round(self.max_position_exposure * 100, 1),
        }

    # ─── Private ──────────────────────────────────────────────────────────────

    def _is_paused(self) -> bool:
        if self.paused_until is None:
            return False
        if datetime.now() >= self.paused_until:
            self.paused_until = None
            return False
        return True

    def _check_new_day(self) -> None:
        today = date.today()
        if self.day != today:
            self.reset_for_new_day(today)
