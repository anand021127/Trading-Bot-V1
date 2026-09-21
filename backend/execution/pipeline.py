"""Shared Strategy → Signal → Risk → Contract → Execution pipeline."""
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

    def submit_signal(self, signal: dict) -> PipelineResult:
        if self.kill.blocks_entries():
            return PipelineResult(False, f"kill_switch={self.kill.level()}")
        if self.require_live_token:
            try:
                assert_token_usable(self.token, context="pipeline")
            except TokenGuardError as exc:
                return PipelineResult(False, str(exc))

        broker_book = fetch_positions(self.client) if self.client is not None else PositionsResult(True, [], None)
        if self.client is not None and not broker_book.ok:
            return PipelineResult(False, f"broker_positions_unavailable:{broker_book.error}")

        sid = make_signal_id(
            strategy=self.strategy.name,
            timestamp=str(signal.get("timestamp")),
            instrument=str(signal.get("instrument_key") or signal.get("symbol")),
            direction=str(signal.get("option_type") or signal.get("side")),
        )
        remembered = self.intents.remember_intent(sid, {"signal": {k: str(v) for k, v in signal.items() if k != "token"}})
        if remembered["duplicate"]:
            return PipelineResult(False, "duplicate_signal", signal_id=sid)

        val = validate_option_contract(
            underlying=str(signal.get("underlying") or "NIFTY50"),
            instrument_key=str(signal.get("instrument_key") or ""),
            strike=float(signal.get("strike") or 0),
            option_type=str(signal.get("option_type") or "CE"),
            expiry_date=str(signal.get("expiry") or ""),
            lot_size=int(signal.get("lot_size") or 0),
            option_ltp=float(signal.get("premium") or 0),
            underlying_spot=float(signal.get("spot") or 0),
            quote_age_seconds=float(signal.get("quote_age_seconds") or 0),
            quantity=int(signal.get("quantity") or 0),
            stop_loss=float(signal.get("stop_loss") or 0),
        )
        if not val.is_valid:
            logger.info("REJECT signal_id=%s reasons=%s", sid, val.reasons)
            return PipelineResult(False, ";".join(val.reasons), signal_id=sid)

        guard = evaluate_pretrade_guard(
            premium=float(signal.get("premium") or 0),
            stop_loss=float(signal.get("stop_loss") or 0),
            quantity=int(signal.get("quantity") or 0),
            lot_size=int(signal.get("lot_size") or 0),
            config=self.risk,
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
        """Controlled exit path. Entries may be blocked; exits still go through
        intentional order placement with idempotency. FULL_SYSTEM_STOP still
        allows flatten exits so positions can be closed.
        """
        level = self.kill.level()
        # Only block exits if we cannot trust state — use same broker check
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
