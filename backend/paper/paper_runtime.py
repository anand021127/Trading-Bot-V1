"""Controlled Paper-mode runtime. Fails closed without explicit config."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from backend.broker.positions_api import fetch_positions
from backend.config.strategy_registry import load_strategy
from backend.database.db_manager import DatabaseManager
from backend.execution.eod import is_past_square_off
from backend.execution.kill_switch import PersistentKillSwitch
from backend.execution.pipeline import ExecutionPipeline
from backend.execution.token_guard import TokenGuardError, assert_token_usable
from backend.orders.idempotency import make_signal_id
from backend.orders.order_models import Order, OrderStatus
from backend.paper.paper_broker import PaperBroker
from backend.risk.risk_config import AuthoritativeRiskConfig, RiskConfigError, build_authoritative_risk_config

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class PaperStartupError(RuntimeError):
    pass


def require_paper_env() -> Dict[str, str]:
    strategy = os.environ.get("TRADING_STRATEGY", "").strip()
    product = os.environ.get("UPSTOX_ORDER_PRODUCT", "").strip().upper()
    mode = os.environ.get("TRADING_MODE", "paper").strip().lower()
    if mode != "paper":
        raise PaperStartupError(f"Paper runtime requires TRADING_MODE=paper, got {mode!r}")
    if strategy != "V8_D_PULLBACK_ATM":
        raise PaperStartupError(
            "Paper mode requires TRADING_STRATEGY=V8_D_PULLBACK_ATM. "
            "Refusing silent OPTION_PREMIUM fallback."
        )
    if product not in {"I", "D"}:
        raise PaperStartupError(
            "Paper mode requires explicit UPSTOX_ORDER_PRODUCT=I or D. "
            "Refusing broker default."
        )
    return {"strategy": strategy, "product": product, "mode": mode}


class PaperTradingRuntime:
    def __init__(self, db: Optional[DatabaseManager] = None, broker: Optional[PaperBroker] = None) -> None:
        env = require_paper_env()
        self.env = env
        self.db = db or DatabaseManager(os.environ.get("DATABASE_PATH", "data/trading_bot.db"))
        self.db.init_db()
        self.broker = broker or PaperBroker()
        self.strategy = load_strategy(env["strategy"])
        capital = float(os.environ.get("TRADING_CAPITAL", "100000"))
        risk_pct = float(os.environ.get("RISK_PER_TRADE_PCT", "0.025"))
        try:
            self.risk = build_authoritative_risk_config(
                capital=capital,
                strategy_risk_pct=float(getattr(self.strategy, "max_account_risk_pct", risk_pct)),
                engine_risk_pct=risk_pct,
                risk_manager_daily_loss_pct=float(os.environ.get("MAX_DAILY_LOSS_PCT", "0.02")),
                configured_risk_pct=risk_pct,
                allocation_limit_pct=float(os.environ.get("MAX_ALLOCATION_PCT", "0.18")),
                max_daily_trades=int(os.environ.get("MAX_TRADES_PER_DAY", "3")),
                max_positions=int(os.environ.get("MAX_CONCURRENT_POSITIONS", "1")),
                max_daily_loss_pct=float(os.environ.get("MAX_DAILY_LOSS_PCT", "0.02")),
                lot_size_source="contract_metadata",
                order_product=env["product"],
                strategy_name=env["strategy"],
                eod_square_off=os.environ.get("EOD_SQUARE_OFF", "15:15"),
            )
        except RiskConfigError as exc:
            raise PaperStartupError(str(exc)) from exc
        self.kill = PersistentKillSwitch(self.db)
        self.pipeline = ExecutionPipeline(
            strategy_name=env["strategy"],
            risk=self.risk,
            db=self.db,
            place_order_fn=self._place,
            client=self.broker,
            require_live_token=False,
        )
        self.eod_done_for: Optional[str] = None
        self.now_fn = lambda: datetime.now(IST)
        self._log_startup()

    def _log_startup(self) -> None:
        logger.info("ACTIVE STRATEGY: %s", self.env["strategy"])
        logger.info("MODE: PAPER")
        logger.info(
            "RISK capital=%s risk_per_trade=%s allocation=%s max_positions=%s "
            "max_daily_trades=%s max_daily_loss=%s lot_size_source=%s product=%s",
            self.risk.capital, self.risk.risk_per_trade_pct, self.risk.allocation_limit_pct,
            self.risk.max_positions, self.risk.max_daily_trades, self.risk.max_daily_loss_pct,
            self.risk.lot_size_source, self.risk.order_product,
        )

    def _place(self, signal: dict, signal_id: str) -> Order:
        book = fetch_positions(self.broker)
        if not book.ok:
            raise RuntimeError(f"broker_positions_unavailable:{book.error}")
        order = self.broker.place_order(
            instrument_key=str(signal["instrument_key"]),
            side="BUY",
            quantity=int(signal["quantity"]),
            price=float(signal["premium"]),
        )
        status = {
            "FILLED": OrderStatus.FILLED,
            "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
            "REJECTED": OrderStatus.REJECTED,
            "UNKNOWN": OrderStatus.UNKNOWN,
            "OPEN": OrderStatus.OPEN,
        }.get(order.status, OrderStatus.UNKNOWN)
        if status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) and order.filled_qty > 0:
            import uuid
            from backend.database.models import Position, Trade

            trade_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc)
            self.db.insert_trade(
                Trade(
                    id=trade_id,
                    symbol=str(signal.get("underlying") or order.instrument_key),
                    side="BUY",
                    quantity=order.filled_qty,
                    price=order.avg_price,
                    timestamp=now,
                    strategy=self.env["strategy"],
                    status="filled",
                    pnl=None,
                    notes=f"signal_id={signal_id} instrument={order.instrument_key}",
                )
            )
            self.db.upsert_position(
                Position(
                    symbol=order.instrument_key,
                    quantity=order.filled_qty,
                    average_price=order.avg_price,
                    entry_time=now,
                    instrument_key=order.instrument_key,
                )
            )
        self._audit({
            "signal_id": signal_id,
            "strategy": self.env["strategy"],
            "decision": order.status,
            "fill_quantity": order.filled_qty,
            "fill_price": order.avg_price,
            **{k: signal.get(k) for k in (
                "timestamp", "underlying", "option_type", "strike", "expiry",
                "premium", "stop_loss", "target", "quantity", "lot_size", "atr",
            )},
        })
        return Order(
            id=order.order_id,
            symbol=order.instrument_key,
            status=status,
            filled_quantity=order.filled_qty,
            remaining_quantity=max(0, order.requested_qty - order.filled_qty),
            quantity=order.requested_qty,
            price=order.avg_price,
            average_price=order.avg_price,
        )

    def _audit(self, row: Dict[str, Any]) -> None:
        safe = {k: v for k, v in row.items() if "token" not in str(k).lower()}
        self.db.save_setting("last_audit", json.dumps(safe, default=str))
        logger.info("PAPER_AUDIT %s", json.dumps(safe, default=str))

    def submit_entry(self, signal: dict) -> Any:
        if self.kill.blocks_entries():
            return type("R", (), {"accepted": False, "reason": f"kill_switch={self.kill.level()}"})()
        if is_past_square_off(self.now_fn(), self.risk.eod_square_off):
            return type("R", (), {"accepted": False, "reason": "EOD_CUTOFF"})()
        token = os.environ.get("UPSTOX_ACCESS_TOKEN", "")
        if token:
            try:
                assert_token_usable(token, context="paper_entry")
            except TokenGuardError as exc:
                return type("R", (), {"accepted": False, "reason": str(exc)})()
        return self.pipeline.submit_signal(signal)

    def reconcile(self) -> Dict[str, Any]:
        book = fetch_positions(self.broker)
        if not book.ok:
            return {"ok": False, "action": "STOP_NEW_ENTRIES", "error": book.error}
        local = {p.symbol: p.quantity for p in self.db.get_open_positions()}
        remote = {p["instrument_key"]: int(p["quantity"]) for p in book.positions}
        if local != remote:
            self.kill.set_level("STOP_NEW_ENTRIES", "position_mismatch")
            return {"ok": False, "action": "STOP_NEW_ENTRIES", "local": local, "remote": remote}
        return {"ok": True, "local": local, "remote": remote}

    def run_eod(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or datetime.now(IST)
        if not is_past_square_off(now, self.risk.eod_square_off):
            return {"ran": False, "reason": "before_cutoff"}
        day = now.date().isoformat()
        if self.eod_done_for == day:
            return {"ran": False, "reason": "already_done"}
        for pos in list(self.broker.positions.values()):
            self.broker.place_order(
                instrument_key=pos["instrument_key"],
                side="SELL" if pos["quantity"] > 0 else "BUY",
                quantity=abs(int(pos["quantity"])),
                price=float(pos["average_price"]),
            )
            self.db.delete_position(pos["instrument_key"])
        self.broker.close_all()
        self.eod_done_for = day
        self._audit({"signal_id": f"EOD-{day}", "decision": "EOD", "exit_reason": "EOD", "fill_quantity": 0})
        return {"ran": True, "remaining": len(self.broker.positions)}
