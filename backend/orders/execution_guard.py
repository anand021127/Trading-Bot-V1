"""Pre-submission risk / sizing / lot checks. Does not alter strategy signals."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from backend.risk.risk_config import AuthoritativeRiskConfig


@dataclass
class GuardDecision:
    allowed: bool
    reasons: List[str] = field(default_factory=list)


def evaluate_pretrade_guard(
    *,
    premium: float,
    stop_loss: float,
    quantity: int,
    lot_size: Optional[int],
    config: AuthoritativeRiskConfig,
    open_positions: int = 0,
    trades_today: int = 0,
    equity: Optional[float] = None,
    daily_realized_pnl: float = 0.0,
) -> GuardDecision:
    reasons: List[str] = []
    if premium is None or float(premium) <= 0:
        reasons.append("premium must be > 0")
    if quantity is None or int(quantity) <= 0:
        reasons.append("quantity is invalid")
    if not lot_size or int(lot_size) <= 1:
        reasons.append("INVALID_LOT_SIZE")
    elif int(quantity) % int(lot_size) != 0:
        reasons.append("quantity is not a multiple of lot size")

    effective_equity = float(equity) if equity is not None else float(config.capital)

    if premium and stop_loss is not None and quantity:
        max_loss = abs(float(premium) - float(stop_loss)) * int(quantity)
        cap = effective_equity * config.risk_per_trade_pct
        if max_loss - cap > 1e-6:
            reasons.append(
                f"estimated max loss {max_loss:.2f} exceeds risk limit {cap:.2f}"
            )
        notional = float(premium) * int(quantity)
        alloc = effective_equity * config.allocation_limit_pct
        if notional - alloc > 1e-6:
            reasons.append(
                f"notional {notional:.2f} exceeds allocation limit {alloc:.2f}"
            )
        if notional - effective_equity > 1e-6:
            reasons.append("INSUFFICIENT_EQUITY")

    if open_positions >= config.max_positions:
        reasons.append("MAX_POSITIONS")
    if trades_today >= config.max_daily_trades:
        reasons.append("MAX_DAILY_TRADES")

    daily_loss_limit = effective_equity * config.max_daily_loss_pct
    if daily_realized_pnl <= -daily_loss_limit and daily_loss_limit > 0:
        reasons.append("MAX_DAILY_LOSS")

    return GuardDecision(allowed=len(reasons) == 0, reasons=reasons)
