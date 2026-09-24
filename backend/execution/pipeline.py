"""Shared Strategy → Signal → Risk → Contract → Execution pipeline.

Paper and any future live path must submit through this single gate.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from backend.broker.positions_api import PositionsResult, fetch_positions
from backend.config.strategy_registry import load_strategy
from backend.execution.kill_switch import PersistentKillSwitch
from backend.execution.token_guard import TokenGuardError, assert_token_usable
from backend.orders.contract_validator import validate_option_contract
from backend.orders.execution_guard import evaluate_pretrade_guard
from backend.orders.idempotency import IdempotentOrderStore, make_signal_id
from backend.risk.risk_config import AuthoritativeRiskConfig

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    accepted: bool
    reason: str
    signal_id: Optional[str] = None
    order: Any = None


class ExecutionPipeline:
    def __init__(
        self,
        *,
        strategy_name: str,
        risk: AuthoritativeRiskConfig,
        db: Any,
        place_order_fn: Callable,
        client: Any = None,
        token: Optional[str] = None,
        require_live_token: bool = False,
        state_provider: Optional[Callable[[], dict]] = None,
    ) -> None:
        self.strategy = load_strategy(strategy_name)
        self.risk = risk
        self.db = db
        self.place_order_fn = place_order_fn
        self.client = client
        self.token = token
        self.require_live_token = require_live_token
        self.intents = IdempotentOrderStore(db)
        self.kill = PersistentKillSwitch(db)
        self.state_provider = state_provider

    def _state(self) -> dict:
        if self.state_provider is not None:
            try:
                return dict(self.state_provider() or {})
            except Exception as exc:
                logger.warning("state_provider failed: %s", exc)
        return {}

    def submit_signal(self, signal: dict) -> PipelineResult:
        if self.kill.blocks_entries():
            return PipelineResult(False, f"kill_switch={self.kill.level()}")
        if self.require_live_token:
            try:
                assert_token_usable(self.token, context="pipeline")
            except TokenGuardError as exc:
                return PipelineResult(False, str(exc))

        # Strategy identity — refuse non-configured strategy payloads
        payload_strategy = str(signal.get("strategy") or signal.get("strategy_name") or "").strip()
        if payload_strategy and payload_strategy != self.strategy.name:
            return PipelineResult(
                False,
                f"INVALID_STRATEGY — pipeline expects {self.strategy.name}, got {payload_strategy}",
            )

        broker_book = fetch_positions(self.client) if self.client is not None else PositionsResult(True, [], None)
        if self.client is not None and not broker_book.ok:
            return PipelineResult(False, f"broker_positions_unavailable:{broker_book.error}")

        state = self._state()
        open_positions = int(state.get("open_positions") or 0)
        trades_today = int(state.get("trades_today") or 0)
        equity = float(state.get("equity") or self.risk.capital)
        daily_realized = float(state.get("daily_realized_pnl") or 0.0)
        daily_loss_pct = float(state.get("daily_loss_pct") or 0.0)

        # Central risk limits with live state
        if open_positions >= self.risk.max_positions:
            return PipelineResult(False, "MAX_POSITIONS")
        if trades_today >= self.risk.max_daily_trades:
            return PipelineResult(False, "MAX_DAILY_TRADES")
        if daily_loss_pct >= self.risk.max_daily_loss_pct - 1e-12 and daily_realized < 0:
            return PipelineResult(False, "MAX_DAILY_LOSS")

        # Duplicate underlying protection when broker positions available
        underlying = str(signal.get("underlying") or "")
        if underlying and self.client is not None and broker_book.ok:
            for p in broker_book.positions:
                # Paper broker details may include underlying
                if str(p.get("underlying") or "") == underlying and int(p.get("quantity") or 0) != 0:
                    return PipelineResult(False, "DUPLICATE_POSITION")

        sid = make_signal_id(
            strategy=self.strategy.name,
            timestamp=str(signal.get("timestamp")),
            instrument=str(signal.get("instrument_key") or signal.get("symbol")),
            direction=str(signal.get("option_type") or signal.get("side")),
        )
        remembered = self.intents.remember_intent(sid, {"signal": {k: str(v) for k, v in signal.items() if k != "token"}})
        if remembered["duplicate"]:
            return PipelineResult(False, "duplicate_signal", signal_id=sid)

        lot_size = int(signal.get("lot_size") or 0)
        quantity = int(signal.get("quantity") or 0)
        if lot_size <= 1:
            return PipelineResult(False, "INVALID_LOT_SIZE", signal_id=sid)
        if quantity <= 0 or quantity % lot_size != 0:
            return PipelineResult(False, "INVALID_QUANTITY", signal_id=sid)

        premium = float(signal.get("premium") or 0)
        if premium <= 0:
            return PipelineResult(False, "INVALID_CONTRACT", signal_id=sid)

        # Sufficient equity for notional
        notional = premium * quantity
        if equity > 0 and notional - equity > 1e-6:
            return PipelineResult(False, "INSUFFICIENT_EQUITY", signal_id=sid)

        val = validate_option_contract(
            underlying=str(signal.get("underlying") or "NIFTY50"),
            instrument_key=str(signal.get("instrument_key") or ""),
            strike=float(signal.get("strike") or 0),
            option_type=str(signal.get("option_type") or "CE"),
            expiry_date=str(signal.get("expiry") or ""),
            lot_size=lot_size,
            option_ltp=premium,
            underlying_spot=float(signal.get("spot") or 0),
            quote_age_seconds=float(signal.get("quote_age_seconds") or 0),
            quantity=quantity,
            stop_loss=float(signal.get("stop_loss") or 0),
        )
        if not val.is_valid:
            logger.info("REJECT signal_id=%s reasons=%s", sid, val.reasons)
            reason = ";".join(val.reasons)
            if "lot size" in reason.lower():
                return PipelineResult(False, f"INVALID_LOT_SIZE:{reason}", signal_id=sid)
            return PipelineResult(False, f"INVALID_CONTRACT:{reason}", signal_id=sid)

        guard = evaluate_pretrade_guard(
            premium=premium,
            stop_loss=float(signal.get("stop_loss") or 0),
            quantity=quantity,
            lot_size=lot_size,
            config=self.risk,
            open_positions=open_positions,
            trades_today=trades_today,
            equity=equity,
            daily_realized_pnl=daily_realized,
        )
        if not guard.allowed:
            logger.info("REJECT signal_id=%s reasons=%s", sid, guard.reasons)
            return PipelineResult(False, ";".join(guard.reasons), signal_id=sid)

        order = self.place_order_fn(signal, sid)
        if getattr(order, "id", None):
            self.intents.mark_submitted(sid, str(order.id))
        logger.info(
            "ORDER strategy=%s signal_id=%s instrument=%s qty=%s order_id=%s status=%s",
            self.strategy.name, sid, signal.get("instrument_key"), signal.get("quantity"),
            getattr(order, "id", None), getattr(order, "status", None),
        )
        return PipelineResult(True, "submitted", signal_id=sid, order=order)

    def submit_exit(self, exit_signal: dict) -> PipelineResult:
        """Controlled exit path. FULL_SYSTEM_STOP still allows flatten exits."""
        level = self.kill.level()
        broker_book = fetch_positions(self.client) if self.client is not None else PositionsResult(True, [], None)
        if self.client is not None and not broker_book.ok:
            return PipelineResult(False, f"broker_positions_unavailable:{broker_book.error}")

        sid = make_signal_id(
            strategy=self.strategy.name,
            timestamp=str(exit_signal.get("timestamp")),
            instrument=str(exit_signal.get("instrument_key") or exit_signal.get("symbol")),
            direction=f"EXIT-{exit_signal.get('side') or 'SELL'}-{exit_signal.get('reason') or ''}",
        )
        remembered = self.intents.remember_intent(
            sid, {"exit": {k: str(v) for k, v in exit_signal.items() if k != "token"}}
        )
        if remembered["duplicate"]:
            return PipelineResult(False, "duplicate_exit", signal_id=sid)

        order = self.place_order_fn(exit_signal, sid)
        if getattr(order, "id", None):
            self.intents.mark_submitted(sid, str(order.id))
        logger.info(
            "EXIT_ORDER strategy=%s signal_id=%s instrument=%s qty=%s reason=%s status=%s kill=%s",
            self.strategy.name, sid, exit_signal.get("instrument_key"),
            exit_signal.get("quantity"), exit_signal.get("reason"),
            getattr(order, "status", None), level,
        )
        return PipelineResult(True, "exit_submitted", signal_id=sid, order=order)
