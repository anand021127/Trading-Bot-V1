"""Controlled Paper-mode runtime. Fails closed without explicit config.

Paper exit engine evaluates SL / TARGET / TRAILING_STOP against subsequent
real option mark prices. EOD uses the latest valid mark — never entry price
as an artificial exit unless that is genuinely the latest mark.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from backend.backtest.engine import CostConfig
from backend.broker.positions_api import fetch_positions
from backend.config.strategy_registry import load_strategy
from backend.database.db_manager import DatabaseManager
from backend.database.models import Position, Trade
from backend.domain.trade_metadata import normalize_trade_metadata
from backend.execution.eod import is_past_square_off
from backend.execution.kill_switch import PersistentKillSwitch
from backend.execution.pipeline import ExecutionPipeline
from backend.execution.token_guard import TokenGuardError, assert_token_usable
from backend.orders.order_models import Order, OrderStatus
from backend.paper.paper_broker import PaperBroker
from backend.risk.risk_config import AuthoritativeRiskConfig, RiskConfigError, build_authoritative_risk_config
from backend.strategy.exit_manager import TrailingStopManager

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

_PENDING_EXIT_KEY = "paper_pending_exit_queue"


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
            state_provider=self.get_risk_state,
        )
        self.eod_done_for: Optional[str] = None
        self.now_fn = lambda: datetime.now(IST)
        self.starting_capital = capital
        self.realized_equity = capital
        self.realized_pnl_total = 0.0
        self.charges_total = 0.0
        self.trades_today = 0
        self.daily_realized_pnl = 0.0
        self._trade_day: Optional[str] = None
        self._closed_instruments: set = set()
        self.cost_model = CostConfig()
        self.trailing = TrailingStopManager()
        # Recover durable paper positions into memory on startup (no Upstox call).
        try:
            self._hydrate_paper_broker_from_db()
        except Exception as exc:
            logger.warning("Paper startup hydrate deferred: %s", exc)
        self._pending_entry_meta: Dict[str, Any] = {}
        self._restore_persistent_day_state()
        self._log_startup()

    # ── durable in-memory trading-state recovery (restart safety) ───────────
    def _restore_persistent_day_state(self) -> None:
        """Restore realized equity and today's risk counters from SQLite.

        Restart-safety: without this, a worker restart after a loss would
        silently reset realized_equity back to starting capital and zero
        trades_today — letting the bot trade past MAX_TRADES_PER_DAY and
        MAX_DAILY_LOSS purely by restarting.
        """
        try:
            snap = self.db.get_setting("paper_equity_snapshot", "")
            if snap:
                data = json.loads(snap)
                self.realized_equity = float(data.get("realized_equity", self.starting_capital))
                self.realized_pnl_total = float(data.get("realized_pnl_total", 0.0))
                self.charges_total = float(data.get("charges_total", 0.0))
                self._trade_day = data.get("trade_day") or None
                self.trades_today = int(data.get("trades_today", 0))
                self.daily_realized_pnl = float(data.get("daily_realized_pnl", 0.0))
        except Exception as exc:
            logger.warning("Could not restore persistent equity state: %s", exc)
        # daily_counters is the authoritative durable source for the day's
        # risk counters; reconcile in-memory state against it.
        day = self.now_fn().date().isoformat()
        counters = self.db.get_daily_counters(day)
        if self._trade_day != day:
            # New calendar day: counters restart from the new day's durable row
            # (which begins at zero). Equity / realized totals carry over.
            self._trade_day = day
            self.trades_today = counters["trades_taken"]
            self.daily_realized_pnl = counters["realized_pnl"]
        else:
            if counters["trades_taken"] > self.trades_today:
                self.trades_today = counters["trades_taken"]
            self.daily_realized_pnl = counters["realized_pnl"]
        logger.info(
            "Paper state restored: equity=%.2f trades_today=%d day=%s",
            self.realized_equity, self.trades_today, day,
        )

    def _persist_day_state(self) -> None:
        """Persist equity + day counters so a crash cannot reset risk state."""
        try:
            self.db.save_setting("paper_equity_snapshot", json.dumps({
                "realized_equity": self.realized_equity,
                "realized_pnl_total": self.realized_pnl_total,
                "charges_total": self.charges_total,
                "trade_day": self._trade_day or self.now_fn().date().isoformat(),
                "trades_today": self.trades_today,
                "daily_realized_pnl": self.daily_realized_pnl,
            }))
        except Exception as exc:
            logger.warning("Could not persist equity snapshot: %s", exc)

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

    def get_risk_state(self) -> Dict[str, Any]:
        """Authoritative trading state for the central pre-trade risk gate."""
        self._roll_day_if_needed()
        open_positions = len([p for p in self.broker.positions.values() if int(p.get("quantity") or 0) != 0])
        unrealized = sum(float(p.get("unrealized_pnl") or 0) for p in self.broker.positions.values())
        return {
            "equity": self.realized_equity,
            "starting_equity": self.starting_capital,
            "open_positions": open_positions,
            "trades_today": self.trades_today,
            "daily_realized_pnl": self.daily_realized_pnl,
            "daily_unrealized_pnl": unrealized,
            "daily_loss_pct": (
                abs(min(0.0, self.daily_realized_pnl)) / self.starting_capital
                if self.starting_capital > 0 else 0.0
            ),
            "strategy": self.env["strategy"],
            "kill_level": self.kill.level(),
        }

    def equity_snapshot(self) -> Dict[str, float]:
        unrealized = sum(float(p.get("unrealized_pnl") or 0) for p in self.broker.positions.values())
        return {
            "starting_capital": self.starting_capital,
            "realized_equity": round(self.realized_equity, 2),
            "realized_pnl": round(self.realized_pnl_total, 2),
            "unrealized_pnl": round(unrealized, 2),
            "total_pnl": round(self.realized_pnl_total + unrealized, 2),
            "charges": round(self.charges_total, 2),
            "mark_to_market_equity": round(self.realized_equity + unrealized, 2),
        }

    def _roll_day_if_needed(self) -> None:
        day = self.now_fn().date().isoformat()
        if self._trade_day != day:
            self._trade_day = day
            # Durable counters are per-day rows; the new day naturally starts at
            # zero. In-memory counters reset to match the new day's ledger row.
            self.trades_today = 0
            self.daily_realized_pnl = 0.0
            self._persist_day_state()

    def _place(self, signal: dict, signal_id: str) -> Order:
        book = fetch_positions(self.broker)
        if not book.ok:
            raise RuntimeError(f"broker_positions_unavailable:{book.error}")

        side = str(signal.get("side") or "BUY").upper()
        is_exit = side == "SELL" or str(signal.get("reason") or "").startswith(
            ("STOP", "TARGET", "TRAILING", "EOD", "MANUAL", "RISK")
        ) or signal.get("is_exit")

        meta = {
            "underlying": signal.get("underlying"),
            "option_type": signal.get("option_type"),
            "strike": signal.get("strike"),
            "expiry": signal.get("expiry"),
            "lot_size": signal.get("lot_size"),
            "stop_loss": signal.get("stop_loss"),
            "target": signal.get("target"),
            "timestamp": signal.get("timestamp"),
            "entry_time": signal.get("timestamp"),
            "strategy": self.env["strategy"],
            "trade_id": signal.get("trade_id") or signal_id,
            "exit_reason": signal.get("reason"),
        }

        order = self.broker.place_order(
            instrument_key=str(signal["instrument_key"]),
            side=side,
            quantity=int(signal["quantity"]),
            price=float(signal.get("premium") or signal.get("price") or 0),
            meta=meta if not is_exit else None,
        )
        status = {
            "FILLED": OrderStatus.FILLED,
            "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
            "REJECTED": OrderStatus.REJECTED,
            "UNKNOWN": OrderStatus.UNKNOWN,
            "OPEN": OrderStatus.OPEN,
        }.get(order.status, OrderStatus.UNKNOWN)

        if not is_exit and status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) and order.filled_qty > 0:
            trade_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc)
            # ONE common trade metadata model — built from the authoritative
            # contract metadata the signal carried + the ACTUAL simulated fill
            # (order.avg_price × order.filled_qty). normalize_trade_metadata
            # recomputes capital_used = entry_price × executed_quantity and
            # leaves anything genuinely absent as NULL (never invented).
            trade_meta = normalize_trade_metadata({
                "underlying": signal.get("underlying"),
                "option_type": signal.get("option_type"),
                "strike": signal.get("strike"),
                "expiry": signal.get("expiry"),
                "instrument_key": order.instrument_key,
                "entry_price": order.avg_price,
                "quantity": order.filled_qty,
                "lot_size": signal.get("lot_size"),
                "entry_timestamp": meta.get("entry_time") or now.isoformat(),
                "strategy": self.env["strategy"],
                "status": "open",
                "trade_id": trade_id,
                "order_id": order.order_id,
                "signal_id": signal_id,
            })
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
                    trade_metadata={
                        "underlying_symbol": trade_meta["underlying_symbol"],
                        "option_type": trade_meta["option_type"],
                        "strike_price": trade_meta["strike_price"],
                        "expiry": trade_meta["expiry"],
                        "instrument_key": trade_meta["instrument_key"],
                        "lot_size": trade_meta["lot_size"],
                        "capital_used": trade_meta["capital_used"],
                        "order_id": trade_meta["order_id"],
                        "signal_id": trade_meta["signal_id"],
                    },
                )
            )
            # Extended entry details (entry_price/entry_time columns) — same
            # as the live path; keeps Trade History consistent across modes.
            try:
                self.db.record_trade_entry_details(
                    trade_id=trade_id,
                    entry_time=trade_meta["entry_timestamp"] or now.isoformat(),
                    entry_price=order.avg_price,
                )
            except Exception:
                logger.debug("record_trade_entry_details failed", exc_info=True)
            # Persist the FULL exit-risk state with the position so a restart
            # can restore SL/target/lot/trade_id exactly (production incident:
            # restart lost stop/target and exits mis-behaved until a quote fix).
            extra = {
                "stop_loss": float(signal.get("stop_loss") or 0),
                "target": float(signal.get("target") or 0),
                "lot_size": int(signal.get("lot_size") or 0),
                "trade_id": trade_id,
                "strategy": self.env["strategy"],
                "underlying": signal.get("underlying"),
                "option_type": signal.get("option_type"),
                "strike": signal.get("strike"),
                "expiry": signal.get("expiry"),
                "entry_time": meta.get("entry_time") or now.isoformat(),
            }
            self.db.upsert_position(
                Position(
                    symbol=order.instrument_key,
                    quantity=order.filled_qty,
                    average_price=order.avg_price,
                    entry_time=now,
                    instrument_key=order.instrument_key,
                    extra=extra,
                )
            )
            # Enrich broker position with SL/target from signal
            pos = self.broker.positions.get(order.instrument_key)
            if pos is not None:
                pos["stop_loss"] = float(signal.get("stop_loss") or pos.get("stop_loss") or 0)
                pos["target"] = float(signal.get("target") or pos.get("target") or 0)
                pos["trailing_stop"] = float(signal.get("stop_loss") or pos.get("trailing_stop") or 0)
                pos["initial_stop"] = float(signal.get("stop_loss") or 0)
                pos["lot_size"] = int(signal.get("lot_size") or pos.get("lot_size") or 0)
                pos["trade_id"] = trade_id
                pos["strategy"] = self.env["strategy"]
                pos["underlying"] = signal.get("underlying")
                pos["option_type"] = signal.get("option_type")
                pos["strike"] = signal.get("strike")
                pos["expiry"] = signal.get("expiry")
            self._roll_day_if_needed()
            self.trades_today += 1
            try:
                self.db.add_daily_trades(self._trade_day or self.now_fn().date().isoformat(), 1)
            except Exception:
                logger.warning("Could not persist daily trade counter", exc_info=True)
            self._persist_day_state()

        self._audit({
            "signal_id": signal_id,
            "strategy": self.env["strategy"],
            "decision": order.status,
            "fill_quantity": order.filled_qty,
            "fill_price": order.avg_price,
            "is_exit": bool(is_exit),
            "exit_reason": signal.get("reason"),
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

    # ── cross-process manual exit queue (API → worker) ─────────────────────
    def queue_manual_exit(self, instrument_key: str, reason: str = "MANUAL_EXIT") -> Dict[str, Any]:
        """Queue a manual exit for the paper worker to execute.

        Production bug fixed: POST /api/positions/{symbol}/exit used to return
        {"status": "exit_queued"} while doing literally nothing — no position
        was ever closed, and the open position silently stayed open (or closed
        only at EOD). Two hard requirements drove this design:
        1. Only the worker process may execute exits (it owns PaperBroker and
           the risk engine) — so the API enqueues and the worker drains.
        2. The exit executes through the worker's normal exit path, which is
           idempotent (duplicate exits for an already-closed position are
           no-ops), so a duplicated queue entry cannot double-exit.
        """
        ik = str(instrument_key or "").strip()
        if not ik:
            return {"queued": False, "reason": "instrument_key_required"}
        raw = self.db.get_setting(_PENDING_EXIT_KEY, "") or "[]"
        try:
            queue = json.loads(raw)
            if not isinstance(queue, list):
                queue = []
        except Exception:
            queue = []
        if any(isinstance(e, dict) and e.get("instrument_key") == ik for e in queue):
            return {"queued": False, "reason": "exit_already_pending", "instrument_key": ik}
        queue.append({
            "instrument_key": ik,
            "reason": reason or "MANUAL_EXIT",
            "queued_at": datetime.now(timezone.utc).isoformat(),
        })
        self.db.save_setting(_PENDING_EXIT_KEY, json.dumps(queue))
        logger.info("Manual exit queued for %s (reason=%s)", ik, reason)
        return {"queued": True, "instrument_key": ik, "reason": reason or "MANUAL_EXIT"}

    def drain_manual_exit_queue(self) -> int:
        """Worker-side: execute any queued manual exits exactly once."""
        raw = self.db.get_setting(_PENDING_EXIT_KEY, "") or "[]"
        if not raw or raw == "[]":
            return 0
        try:
            queue = json.loads(raw)
            if not isinstance(queue, list):
                queue = []
        except Exception:
            self.db.save_setting(_PENDING_EXIT_KEY, "[]")
            return 0
        executed = 0
        for item in queue:
            if not isinstance(item, dict):
                continue
            ik = str(item.get("instrument_key") or "").strip()
            if not ik:
                continue
            reason = str(item.get("reason") or "MANUAL_EXIT")
            pos = self.broker.positions.get(ik)
            if pos is None or pos.get("closed") or int(pos.get("quantity") or 0) <= 0:
                # Already closed (e.g. EOD or SL ran first) — idempotent no-op.
                logger.info("Manual exit skipped (position not open): %s", ik)
                continue
            mark = float(pos.get("mark_price") or 0)
            if mark <= 0:
                mark = float(pos.get("entry_price") or pos.get("average_price") or 0)
            if mark <= 0:
                # No valid mark ever seen — refuse to invent an exit price.
                logger.warning("Manual exit refused for %s: no valid mark available", ik)
                continue
            result = self._execute_exit(
                instrument_key=ik,
                exit_price=mark,
                reason=reason,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            if result:
                executed += 1
        if queue:
            self.db.save_setting(_PENDING_EXIT_KEY, "[]")
        return executed

    def submit_entry(self, signal: dict) -> Any:
        if self.kill.blocks_entries():
            return type("R", (), {"accepted": False, "reason": f"kill_switch={self.kill.level()}"})()
        if is_past_square_off(self.now_fn(), self.risk.eod_square_off):
            return type("R", (), {"accepted": False, "reason": "EOD_CUTOFF"})()

        # Contract lot-size: metadata is mandatory for paper entries
        lot = int(signal.get("lot_size") or 0)
        qty = int(signal.get("quantity") or 0)
        if lot <= 1:
            return type("R", (), {"accepted": False, "reason": "INVALID_LOT_SIZE"})()
        if qty <= 0 or qty % lot != 0:
            return type("R", (), {"accepted": False, "reason": "INVALID_QUANTITY_NOT_MULTIPLE_OF_LOT"})()

        offline = os.environ.get("TRADING_BOT_OFFLINE_TESTS") == "1"
        token = os.environ.get("UPSTOX_ACCESS_TOKEN", "")
        if token and not offline:
            try:
                assert_token_usable(token, context="paper_entry")
            except TokenGuardError as exc:
                return type("R", (), {"accepted": False, "reason": str(exc)})()
        return self.pipeline.submit_signal(signal)

    def on_option_quote(
        self,
        instrument_key: str,
        mark_price: float,
        timestamp: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Process a subsequent option mark for an open paper position.

        Evaluates TARGET, STOP_LOSS, and TRAILING_STOP using V8-D TrailingStopManager
        rules. Executes at most one exit. Returns exit summary or None.
        """
        if instrument_key in self._closed_instruments:
            return None
        pos = self.broker.update_mark(instrument_key, mark_price)
        if pos is None:
            return None
        if int(pos.get("quantity") or 0) <= 0 or pos.get("closed"):
            return None

        px = float(mark_price)
        entry = float(pos.get("entry_price") or pos.get("average_price") or 0)
        initial_stop = float(pos.get("initial_stop") or pos.get("stop_loss") or 0)
        target = float(pos.get("target") or 0)
        trailing = float(pos.get("trailing_stop") or initial_stop)

        # Ratchet trailing stop using existing V8-D stage rules (never loosens)
        if entry > 0 and initial_stop > 0 and px > entry:
            trail = self.trailing.compute(
                entry_price=entry,
                initial_stop=initial_stop,
                current_price=px,
                current_stop=trailing,
            )
            new_stop = float(trail["stop"])
            if new_stop > trailing:
                pos["trailing_stop"] = new_stop
                trailing = new_stop

        exit_reason = None
        # Conservative deterministic order when both could be true on same tick:
        # check stop first (worst case), then target — documents adverse-fill preference.
        if trailing > 0 and px <= trailing:
            exit_reason = "TRAILING_STOP" if trailing > initial_stop + 1e-9 else "STOP_LOSS"
        elif initial_stop > 0 and px <= initial_stop:
            exit_reason = "STOP_LOSS"
        elif target > 0 and px >= target:
            exit_reason = "TARGET"

        if not exit_reason:
            return None

        return self._execute_exit(
            instrument_key=instrument_key,
            exit_price=px,
            reason=exit_reason,
            timestamp=timestamp,
        )

    def _execute_exit(
        self,
        *,
        instrument_key: str,
        exit_price: float,
        reason: str,
        timestamp: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if instrument_key in self._closed_instruments:
            return None
        pos = self.broker.positions.get(instrument_key)
        if pos is None or pos.get("closed") or int(pos.get("quantity") or 0) <= 0:
            return None

        qty = abs(int(pos["quantity"]))
        entry = float(pos.get("entry_price") or pos.get("average_price") or 0)
        px = float(exit_price)
        if px <= 0:
            # Never invent a price — refuse exit without a valid market mark
            logger.warning("Exit refused for %s: invalid exit_price=%s", instrument_key, exit_price)
            return None

        charges = self.cost_model.apply(entry, px, qty, is_option=True)
        gross = charges["gross_pnl"]
        total_cost = charges["total_cost"]
        net = charges["net_pnl"]

        # Place simulated SELL through pipeline place_order_fn (same broker)
        exit_signal = {
            "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
            "instrument_key": instrument_key,
            "side": "SELL",
            "quantity": qty,
            "premium": px,
            "price": px,
            "reason": reason,
            "is_exit": True,
            "underlying": pos.get("underlying"),
            "option_type": pos.get("option_type"),
            "strike": pos.get("strike"),
            "expiry": pos.get("expiry"),
            "lot_size": pos.get("lot_size"),
            "trade_id": pos.get("trade_id"),
        }
        # Exits always attempt a full fill — do not inherit entry-test fill modes
        prev_mode = getattr(self.broker, "next_fill_mode", "full")
        self.broker.next_fill_mode = "full"
        try:
            order = self.broker.place_order(
                instrument_key=instrument_key,
                side="SELL",
                quantity=qty,
                price=px,
            )
        finally:
            self.broker.next_fill_mode = prev_mode
        if order.filled_qty <= 0:
            return None

        self._closed_instruments.add(instrument_key)
        try:
            self.db.delete_position(instrument_key)
        except Exception:
            pass

        # Persist exit on the SAME trade row created at entry — the common
        # metadata model's entry columns (strike/option_type/expiry/lot_size/
        # instrument_key/capital_used) are left untouched so the closed trade
        # keeps its original entry identity. Only exit-side fields are written
        # (whitelist in update_trade_exit), with net P&L persisted for Trade
        # History (previously pnl= was passed and silently dropped by the
        # whitelist, leaving net_pnl NULL and status stuck at 'filled').
        try:
            if hasattr(self.db, "update_trade_exit") and pos.get("trade_id"):
                self.db.update_trade_exit(
                    pos["trade_id"],
                    exit_price=px,
                    exit_time=timestamp or datetime.now(timezone.utc).isoformat(),
                    exit_reason=reason,
                    gross_pnl=gross,
                    net_pnl=net,
                    brokerage=charges.get("brokerage"),
                    stt=charges.get("stt"),
                    order_id=order.order_id,
                )
        except Exception:
            logger.debug("update_trade_exit not available or failed", exc_info=True)

        self.realized_equity = round(self.realized_equity + net, 2)
        self.realized_pnl_total = round(self.realized_pnl_total + net, 2)
        self.charges_total = round(self.charges_total + total_cost, 2)
        self._roll_day_if_needed()
        self.daily_realized_pnl = round(self.daily_realized_pnl + net, 2)
        try:
            self.db.add_daily_realized_pnl(self._trade_day or self.now_fn().date().isoformat(), net)
        except Exception:
            logger.warning("Could not persist daily realized P&L", exc_info=True)
        self._persist_day_state()

        summary = {
            "instrument_key": instrument_key,
            "exit_reason": reason,
            "entry_price": entry,
            "exit_price": px,
            "quantity": qty,
            "gross_pnl": gross,
            "charges": total_cost,
            "net_pnl": net,
            "realized_equity": self.realized_equity,
            "order_id": order.order_id,
        }
        self._audit({
            "signal_id": f"EXIT-{instrument_key}-{reason}",
            "strategy": self.env["strategy"],
            "decision": "EXIT",
            "exit_reason": reason,
            "fill_quantity": qty,
            "fill_price": px,
            "net_pnl": net,
            "realized_equity": self.realized_equity,
        })
        logger.info(
            "PAPER_EXIT %s reason=%s entry=%.2f exit=%.2f qty=%d net=%.2f equity=%.2f",
            instrument_key, reason, entry, px, qty, net, self.realized_equity,
        )
        return summary

    def _hydrate_paper_broker_from_db(self) -> int:
        """Restore PaperBroker open positions from the durable SQLite paper ledger.

        Paper mode never consults real Upstox positions. After a worker/process
        restart the in-memory PaperBroker is empty while SQLite still holds
        confirmed paper fills — rehydrate so reconcile compares ledger↔broker
        consistently without requiring a matching live Upstox position.
        """
        if not isinstance(self.broker, PaperBroker):
            # Paper runtime must not use a live client as the position book.
            logger.error(
                "Paper reconcile refused non-PaperBroker client type=%s",
                type(self.broker).__name__,
            )
            return 0
        restored = 0
        for pos in self.db.get_open_positions():
            ik = (getattr(pos, "instrument_key", None) or pos.symbol or "").strip()
            if not ik:
                continue
            qty = int(pos.quantity or 0)
            if qty == 0:
                continue
            existing = self.broker.positions.get(ik)
            if existing and not existing.get("closed") and int(existing.get("quantity") or 0) != 0:
                continue  # already present in memory
            entry_time = ""
            if getattr(pos, "entry_time", None) is not None:
                et = pos.entry_time
                entry_time = et.isoformat() if hasattr(et, "isoformat") else str(et)
            extra = getattr(pos, "extra", None) or {}
            if not isinstance(extra, dict):
                extra = {}
            # Full exit-risk restoration: SL/target/lot/trade_id must survive the
            # restart or the position would exit at wrong levels / double-log P&L.
            self.broker.restore_position(
                instrument_key=ik,
                quantity=qty,
                average_price=float(pos.average_price or 0),
                entry_time=entry_time or str(extra.get("entry_time") or ""),
                meta={
                    "stop_loss": extra.get("stop_loss", 0),
                    "target": extra.get("target", 0),
                    "trailing_stop": extra.get("trailing_stop", extra.get("stop_loss", 0)),
                    "lot_size": extra.get("lot_size", 0),
                    "trade_id": extra.get("trade_id", ""),
                    "strategy": extra.get("strategy", self.env.get("strategy", "V8_D_PULLBACK_ATM")),
                    "underlying": extra.get("underlying", ""),
                    "option_type": extra.get("option_type", ""),
                    "strike": extra.get("strike"),
                    "expiry": extra.get("expiry", ""),
                },
            )
            restored += 1
        if restored:
            logger.info("Paper ledger hydrate: restored %d position(s) into PaperBroker from SQLite", restored)
        return restored

    def reconcile(self) -> Dict[str, Any]:
        """Reconcile SQLite paper ledger ↔ in-memory PaperBroker only.

        Never compares paper fills against the real Upstox position book.
        Never places a real order. Genuine ledger↔PaperBroker mismatches and
        broker API failures still raise STOP_NEW_ENTRIES.
        """
        if not isinstance(self.broker, PaperBroker):
            self.kill.set_level("STOP_NEW_ENTRIES", "paper_broker_type_invalid")
            return {
                "ok": False,
                "action": "STOP_NEW_ENTRIES",
                "error": f"paper_requires_PaperBroker_got_{type(self.broker).__name__}",
            }

        # Recover durable paper positions into memory before comparing.
        self._hydrate_paper_broker_from_db()

        book = fetch_positions(self.broker)
        if not book.ok:
            self.kill.set_level("STOP_NEW_ENTRIES", "broker_positions_unavailable")
            return {"ok": False, "action": "STOP_NEW_ENTRIES", "error": book.error}

        local = {p.symbol: int(p.quantity) for p in self.db.get_open_positions() if int(p.quantity or 0) != 0}
        # Normalize keys: SQLite may use symbol == instrument_key for options
        local_norm: Dict[str, int] = {}
        for p in self.db.get_open_positions():
            qty = int(p.quantity or 0)
            if qty == 0:
                continue
            ik = (getattr(p, "instrument_key", None) or p.symbol or "").strip()
            if ik:
                local_norm[ik] = qty

        remote = {
            str(p["instrument_key"]): int(p["quantity"])
            for p in book.positions
            if int(p.get("quantity") or 0) != 0
        }

        if local_norm != remote:
            self.kill.set_level("STOP_NEW_ENTRIES", "position_mismatch")
            logger.warning(
                "Paper position mismatch ledger=%s paper_broker=%s",
                local_norm, remote,
            )
            return {
                "ok": False,
                "action": "STOP_NEW_ENTRIES",
                "local": local_norm,
                "remote": remote,
            }
        return {"ok": True, "local": local_norm, "remote": remote}

    def run_eod(self, now: Optional[datetime] = None, marks: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """Square off remaining open paper positions at latest valid mark prices.

        `marks` maps instrument_key → latest option LTP. If a mark is missing,
        uses position mark_price if it was updated during the session. Never
        falls back to entry/average price unless that is the only recorded mark
        (i.e. no subsequent quote was ever received — then exit is still marked
        EOD_SQUARE_OFF at that mark for determinism).
        """
        now = now or datetime.now(IST)
        if not is_past_square_off(now, self.risk.eod_square_off):
            return {"ran": False, "reason": "before_cutoff"}
        day = now.date().isoformat()
        if self.eod_done_for == day:
            return {"ran": False, "reason": "already_done"}

        marks = marks or {}
        closed: List[Dict[str, Any]] = []
        for ik, pos in list(self.broker.positions.items()):
            if int(pos.get("quantity") or 0) == 0 or pos.get("closed"):
                continue
            mark = marks.get(ik)
            if mark is None or float(mark) <= 0:
                mark = pos.get("mark_price")
            if mark is None or float(mark) <= 0:
                # Last resort: only if no mark was ever observed — still not inventing
                # a different price; use the stored mark/entry that the broker holds.
                mark = pos.get("mark_price") or pos.get("average_price")
            result = self._execute_exit(
                instrument_key=ik,
                exit_price=float(mark),
                reason="EOD_SQUARE_OFF",
                timestamp=now.isoformat(),
            )
            if result:
                closed.append(result)

        self.broker.close_all()
        self.eod_done_for = day
        self._audit({
            "signal_id": f"EOD-{day}",
            "decision": "EOD",
            "exit_reason": "EOD_SQUARE_OFF",
            "closed_count": len(closed),
            "fill_quantity": sum(c.get("quantity", 0) for c in closed),
        })
        return {"ran": True, "remaining": len(self.broker.positions), "closed": closed}
