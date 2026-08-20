"""V8-D Safe Shadow & Paper Trading Execution Engine.

Simulates the exact live execution path with:
- Real market data
- Real option contracts from Upstox option chain
- Real signal generation (V8-D Pullback + Reversal)
- Real risk-capped sizing (Max 3% risk, Max 20% allocation, Dynamic Account Equity)
- NO real order placement at broker

Logs every signal decision (accepted or rejected) with full structural transparency.
Tracks hypothetical fills, stop/target exits, and P&L accounting.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.backtest.engine import CostConfig
from backend.orders.contract_validator import validate_option_contract
from backend.strategy.strategies.v8d_strategy import V8DStrategy, V8DDecisionLog

logger = logging.getLogger(__name__)


@dataclass
class ShadowTrade:
    trade_id: str
    underlying: str
    instrument_key: str
    option_type: str
    strike: float
    expiry_date: str
    entry_time: str
    entry_price: float
    quantity: int
    lot_size: int
    stop_loss: float
    target: float
    position_value: float
    account_risk_pct: float
    capital_alloc_pct: float
    status: str  # OPEN, CLOSED_TARGET, CLOSED_STOP, CLOSED_EOD
    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    gross_pnl: float = 0.0
    total_cost: float = 0.0
    net_pnl: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class V8DShadowEngine:
    def __init__(
        self,
        starting_equity: float = 100000.0,
        log_dir: str = "logs",
        strategy: Optional[V8DStrategy] = None,
    ) -> None:
        self.equity = starting_equity
        self.starting_equity = starting_equity
        self.strategy = strategy or V8DStrategy()
        self.cost_model = CostConfig()
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.decision_logs: List[V8DDecisionLog] = []
        self.open_trades: Dict[str, ShadowTrade] = {}
        self.closed_trades: List[ShadowTrade] = []
        self.trades_today: int = 0

    def evaluate_and_shadow_execute(
        self,
        underlying_symbol: str,
        underlying_candles: List[Dict[str, Any]],
        spot_price: float,
        option_chain: List[Dict[str, Any]],
        quote_age_seconds: float = 1.0,
        kill_switch_active: bool = False,
        reconciliation_ok: bool = True,
    ) -> Optional[ShadowTrade]:
        """Runs the complete V8-D decision pipeline in shadow mode."""
        sig, decision_log = self.strategy.evaluate_v8d_signal(
            underlying_symbol=underlying_symbol,
            underlying_candles=underlying_candles,
            spot_price=spot_price,
            option_chain=option_chain,
            account_equity=self.equity,
            trades_today=self.trades_today,
            kill_switch_active=kill_switch_active,
            reconciliation_ok=reconciliation_ok,
        )
        self.decision_logs.append(decision_log)

        # Log decision to structured audit trail
        self._append_decision_log(decision_log)

        if decision_log.decision != "ACCEPTED" or not sig.indicators:
            return None

        contract = sig.indicators["selected_contract"]
        sizing = sig.indicators["sizing"]

        # Run Critical Pre-Trade Contract Validation Guardrails
        val_result = validate_option_contract(
            underlying=underlying_symbol,
            instrument_key=contract["instrument_key"],
            strike=float(contract["strike"]),
            option_type=contract["option_type"],
            expiry_date=contract.get("expiry") or contract.get("expiry_date", ""),
            lot_size=sig.indicators["lot_size"],
            option_ltp=sig.entry_price,
            underlying_spot=spot_price,
            quote_age_seconds=quote_age_seconds,
            account_equity=self.equity,
            quantity=sizing["quantity"],
            stop_loss=sig.stop_loss,
            reconciliation_ok=reconciliation_ok,
            kill_switch_active=kill_switch_active,
        )

        if not val_result.is_valid:
            decision_log.decision = "REJECTED_BY_GUARDRAIL"
            decision_log.rejection_reasons.extend(val_result.reasons)
            self._append_decision_log(decision_log)
            logger.warning("Shadow order rejected by guardrails: %s", val_result.reasons)
            return None

        # Prevent duplicate positions on same underlying
        if underlying_symbol in self.open_trades:
            logger.info("Shadow trade skipped: Position already open for %s", underlying_symbol)
            return None

        # Generate Shadow Trade
        trade_id = f"SHADOW_V8D_{len(self.closed_trades) + len(self.open_trades) + 1:04d}"
        trade = ShadowTrade(
            trade_id=trade_id,
            underlying=underlying_symbol,
            instrument_key=contract["instrument_key"],
            option_type=contract["option_type"],
            strike=float(contract["strike"]),
            expiry_date=contract.get("expiry") or contract.get("expiry_date", ""),
            entry_time=datetime.now(timezone.utc).isoformat(),
            entry_price=sig.entry_price,
            quantity=sizing["quantity"],
            lot_size=sig.indicators["lot_size"],
            stop_loss=sig.stop_loss,
            target=sig.target,
            position_value=sizing["position_value"],
            account_risk_pct=sizing["actual_risk_pct"],
            capital_alloc_pct=sizing["actual_allocation_pct"],
            status="OPEN",
        )

        self.open_trades[underlying_symbol] = trade
        self.trades_today += 1
        logger.info(
            "V8-D SHADOW POSITION OPENED: %s %s %s @ ₹%.2f | SL: ₹%.2f | Target: ₹%.2f | Qty: %d",
            underlying_symbol, contract["option_type"], contract["strike"],
            trade.entry_price, trade.stop_loss, trade.target, trade.quantity,
        )
        return trade

    def update_quote_and_check_exit(
        self,
        underlying_symbol: str,
        current_option_ltp: float,
        timestamp: Optional[str] = None,
        force_eod: bool = False,
    ) -> Optional[ShadowTrade]:
        """Update live quote for an open shadow position and evaluate target/stop/EOD exits."""
        if underlying_symbol not in self.open_trades:
            return None

        trade = self.open_trades[underlying_symbol]
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        exit_reason = None
        exit_price = current_option_ltp

        if force_eod:
            exit_reason = "EOD_SQUAREOFF"
            trade.status = "CLOSED_EOD"
        elif current_option_ltp >= trade.target:
            exit_reason = "TARGET_HIT"
            trade.status = "CLOSED_TARGET"
        elif current_option_ltp <= trade.stop_loss:
            exit_reason = "STOP_LOSS_HIT"
            trade.status = "CLOSED_STOP"

        if exit_reason:
            trade.exit_time = ts
            trade.exit_price = exit_price
            trade.exit_reason = exit_reason

            charges = self.cost_model.apply(trade.entry_price, exit_price, trade.quantity, is_option=True)
            trade.gross_pnl = charges["gross_pnl"]
            trade.total_cost = charges["total_cost"]
            trade.net_pnl = charges["net_pnl"]

            self.equity += trade.net_pnl
            self.closed_trades.append(trade)
            del self.open_trades[underlying_symbol]

            logger.info(
                "V8-D SHADOW POSITION CLOSED: %s (%s) @ ₹%.2f -> ₹%.2f | Net P&L: ₹%.2f | Equity: ₹%.2f",
                trade.trade_id, exit_reason, trade.entry_price, exit_price, trade.net_pnl, self.equity,
            )
            return trade

        return None

    def _append_decision_log(self, decision_log: V8DDecisionLog):
        try:
            log_file = self.log_dir / "v8d_shadow_decisions.jsonl"
            with open(log_file, "a", encoding="utf-8") as fp:
                fp.write(json.dumps(asdict(decision_log)) + "\n")
        except Exception as e:
            logger.error("Failed to write decision log: %s", e)
