"""Single authoritative risk configuration used by backtest, paper, and live."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


class RiskConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthoritativeRiskConfig:
    capital: float
    risk_per_trade_pct: float
    allocation_limit_pct: float
    max_daily_trades: int
    max_positions: int
    max_daily_loss_pct: float
    lot_size_source: str
    order_product: str
    strategy_name: str
    eod_square_off: str = "15:15"


def _close(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(float(a) - float(b)) <= tol


def build_authoritative_risk_config(
    *,
    capital: float,
    strategy_risk_pct: Optional[float],
    engine_risk_pct: Optional[float],
    risk_manager_daily_loss_pct: Optional[float],
    configured_risk_pct: Optional[float],
    allocation_limit_pct: float,
    max_daily_trades: int,
    max_positions: int,
    max_daily_loss_pct: float,
    lot_size_source: str,
    order_product: str,
    strategy_name: str,
    eod_square_off: str = "15:15",
    allow_unspecified_engine: bool = True,
) -> AuthoritativeRiskConfig:
    """Fail startup when configured risk values conflict.

    Engine default 1% vs strategy 2.5% is a conflict if both are supplied
    and differ. Callers must pass only the values they intend to honor, or
    this function raises.
    """
    candidates = []
    if configured_risk_pct is not None:
        candidates.append(("config", float(configured_risk_pct)))
    if strategy_risk_pct is not None:
        candidates.append(("strategy", float(strategy_risk_pct)))
    if engine_risk_pct is not None:
        candidates.append(("engine", float(engine_risk_pct)))
    if not candidates:
        raise RiskConfigError("No risk_per_trade value provided")

    base_name, base_val = candidates[0]
    for name, val in candidates[1:]:
        if not _close(val, base_val):
            raise RiskConfigError(
                f"Conflicting risk_per_trade values: {base_name}={base_val} vs {name}={val}. "
                "Refusing to pick one silently."
            )

    if capital <= 0:
        raise RiskConfigError("capital must be positive")
    if not lot_size_source:
        raise RiskConfigError("lot_size_source must be set (contract_metadata)")
    if not order_product:
        raise RiskConfigError("order_product must be explicit (I or D)")
    if order_product not in ("I", "D"):
        raise RiskConfigError(f"invalid order_product {order_product}")
    if not strategy_name:
        raise RiskConfigError("strategy_name must be explicit")

    cfg = AuthoritativeRiskConfig(
        capital=float(capital),
        risk_per_trade_pct=float(base_val),
        allocation_limit_pct=float(allocation_limit_pct),
        max_daily_trades=int(max_daily_trades),
        max_positions=int(max_positions),
        max_daily_loss_pct=float(max_daily_loss_pct),
        lot_size_source=lot_size_source,
        order_product=order_product,
        strategy_name=strategy_name,
        eod_square_off=eod_square_off,
    )
    logger.info(
        "RISK_CONFIG capital=%s risk_per_trade=%s allocation_limit=%s max_daily_trades=%s "
        "max_positions=%s max_daily_loss=%s lot_size_source=%s product=%s strategy=%s eod=%s",
        cfg.capital, cfg.risk_per_trade_pct, cfg.allocation_limit_pct, cfg.max_daily_trades,
        cfg.max_positions, cfg.max_daily_loss_pct, cfg.lot_size_source, cfg.order_product,
        cfg.strategy_name, cfg.eod_square_off,
    )
    return cfg
