"""Production trading engine — full ORB strategy with safety controls.

V21-FINAL changes:
  - execute_multi_signal: uses instrument_key for option orders (Item 1)
  - _close_position: exit uses contract instrument_key (Item 17)
  - Lot-risk validation: rejects if 1 lot > allowed risk (Item 10)
  - Exposure enforcement: rejects if exposure limit exceeded (Item 11)
  - Daily floor trade: DISABLED (Item 12)
  - Dynamic WS subscription: subscribe on entry, unsubscribe on exit (Item 3)
  - Tick freshness: stale option ticks prevent entries (Item 4)
  - Position detail: shows contract info, tick age, data status (Item 28)
  - Confidence relabeled as setup_score in output (Item 13)
  - Fill tracking: uses actual filled qty/price for P&L (Items 16, 18)
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from backend.broker.upstox_client import UpstoxClient
from backend.config.settings import load_settings
from backend.database.db_manager import DatabaseManager
from backend.database.models import Position, Trade
from backend.indicators.atr import calculate_atr
from backend.indicators.choppiness import choppiness_index
from backend.indicators.ema import calculate_ema
from backend.indicators.rsi import calculate_rsi
from backend.logging_system.trade_logger import TradeLogger
from backend.notifications.email_alerts import EmailAlerts
from backend.notifications.telegram_alerts import TelegramAlerts
from backend.orders.order_manager import OrderManager, OrderError
from backend.orders.order_models import OrderRequest, OrderStatus
from backend.risk.position_sizer import PositionSizer
from backend.risk.risk_manager import RiskManager
from backend.strategy.exit_manager import ExitManager, TrailingStopManager
from backend.strategy.strategy_engine import MultiStrategyEngine

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
settings = load_settings()

# V21-FINAL: configurable max option tick age in seconds
MAX_OPTION_TICK_AGE_SECONDS = 30


def classify_underlying_trend(candles: List[Dict[str, Any]]) -> str:
    """BULLISH / BEARISH / NEUTRAL from a real candle series — shared by
    live trading (detect_underlying_trend) and the backtest engine, so
    both use the exact same classification instead of two implementations
    drifting apart.

    This is also where the choppy-market filter lives: options-buying
    strategies are especially vulnerable to sideways/range-bound markets
    (theta decay with no directional payoff), so a raw EMA20/EMA50
    crossover alone is not enough — that alone flips on small noise during
    chop, producing frequent low-quality directional calls. The
    Choppiness Index (a standard, published indicator — CI > 61.8 is the
    conventional "choppy, skip" threshold) overrides the EMA reading to
    NEUTRAL whenever the market is genuinely range-bound, regardless of
    which way the EMAs happen to be pointing at that moment.
    """
    from backend.strategy.strategies.ema_trend import EMATrendStrategy

    ema_strat = EMATrendStrategy()
    if len(candles) < ema_strat.min_candles:
        return "NEUTRAL"

    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]
    ci_vals = choppiness_index(highs, lows, closes, 14)
    if ci_vals and ci_vals[-1] > 61.8:
        return "NEUTRAL"

    sig = ema_strat.evaluate("UNDERLYING", candles)
    if sig.conditions.get("ema_trend_up") and sig.conditions.get("price_above_ema20"):
        return "BULLISH"
    if sig.conditions.get("ema_trend_up") is False:
        return "BEARISH"
    return "NEUTRAL"


def build_trend_series(
    candles: List[Dict[str, Any]], ema_fast: int = 20, ema_slow: int = 50, ci_period: int = 14,
) -> Dict[str, str]:
    """A real, no-lookahead BULLISH/BEARISH/NEUTRAL classification for
    EVERY bar in a real underlying candle series, keyed by that bar's own
    timestamp — same logic as classify_underlying_trend(), computed once
    as an aligned array instead of a single point-in-time snapshot.

    This is the fix for a real backtest realism bug: the backtest used to
    fetch only the LATEST 100 underlying bars (as of right now, whenever
    the backtest happens to be run), classify a single static trend from
    that, and apply that one label across the ENTIRE historical date
    range being backtested — meaning a full year of entries could all be
    judged as "the market is BULLISH" (or BEARISH) just because that
    happened to be true today, regardless of how the market actually
    moved during the period under test. This function instead classifies
    every point in time using only data available up to that point.
    """
    if len(candles) < ema_slow:
        return {}
    closes = [float(c["close"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]

    ema_fast_vals = calculate_ema(closes, ema_fast)
    ema_slow_vals = calculate_ema(closes, ema_slow)
    ci_vals = choppiness_index(highs, lows, closes, ci_period)
    ci_offset = len(closes) - len(ci_vals)  # ci_vals[j] aligns to closes[j + ci_offset]

    series: Dict[str, str] = {}
    for i in range(ema_slow - 1, len(closes)):
        ts = candles[i].get("timestamp")
        if not ts:
            continue
        ci_idx = i - ci_offset
        if 0 <= ci_idx < len(ci_vals) and ci_vals[ci_idx] > 61.8:
            series[ts] = "NEUTRAL"
        elif ema_fast_vals[i] > ema_slow_vals[i] and closes[i] > ema_fast_vals[i]:
            series[ts] = "BULLISH"
        elif ema_fast_vals[i] < ema_slow_vals[i]:
            series[ts] = "BEARISH"
        else:
            series[ts] = "NEUTRAL"
    return series


class BotState:
    """Bot running state — persisted to the shared SQLite DB, NOT just an
    in-process class attribute.

    Root cause this fixes: this project runs as TWO separate OS processes
    on Render (the web API and a standalone `backend/worker.py` process
    that actually runs the trading loop). Each process has its own Python
    memory space, so a plain in-process class attribute here would mean
    the dashboard's Start/Stop/Kill controls (hitting the web process)
    have zero effect on the worker process actually placing trades — and
    the dashboard would show whatever the WEB process's own independent
    copy of this state happens to be, not the worker's. Persisting to the
    DB on Render's shared disk (/data) makes both processes agree.
    """
    _db: Any = None  # lazily-constructed shared DatabaseManager

    _KEY_RUNNING = "bot_state_running"
    _KEY_KILL = "bot_state_kill_switch"
    _KEY_START_TIME = "bot_state_start_time"
    _KEY_STOP_REASON = "bot_state_stop_reason"

    @classmethod
    def _get_db(cls) -> Any:
        if cls._db is None:
            from backend.database.db_manager import DatabaseManager
            cls._db = DatabaseManager(db_path=settings.database.path)
        return cls._db

    @classmethod
    def start(cls) -> None:
        db = cls._get_db()
        db.save_setting(cls._KEY_RUNNING, "true")
        db.save_setting(cls._KEY_KILL, "false")
        db.save_setting(cls._KEY_START_TIME, datetime.now(timezone.utc).isoformat())
        db.save_setting(cls._KEY_STOP_REASON, "")

    @classmethod
    def stop(cls, reason: str = "Manual stop") -> None:
        db = cls._get_db()
        db.save_setting(cls._KEY_RUNNING, "false")
        db.save_setting(cls._KEY_STOP_REASON, reason)

    @classmethod
    def kill(cls, reason: str = "Emergency kill switch") -> None:
        db = cls._get_db()
        db.save_setting(cls._KEY_KILL, "true")
        db.save_setting(cls._KEY_RUNNING, "false")
        db.save_setting(cls._KEY_STOP_REASON, reason)
        TradeLogger.log_critical("BotState", f"KILL SWITCH ACTIVATED: {reason}")

    @classmethod
    def reset_kill(cls) -> None:
        cls._get_db().save_setting(cls._KEY_KILL, "false")

    @classmethod
    def is_running(cls) -> bool:
        db = cls._get_db()
        running = db.get_setting(cls._KEY_RUNNING, "false") == "true"
        killed = db.get_setting(cls._KEY_KILL, "false") == "true"
        return running and not killed

    @classmethod
    def status(cls) -> Dict[str, Any]:
        db = cls._get_db()
        running = db.get_setting(cls._KEY_RUNNING, "false") == "true"
        killed = db.get_setting(cls._KEY_KILL, "false") == "true"
        start_time_raw = db.get_setting(cls._KEY_START_TIME, "")
        start_time = datetime.fromisoformat(start_time_raw) if start_time_raw else None
        return {
            "running": running,
            "kill_switch_active": killed,
            "start_time": start_time_raw or None,
            "stop_reason": db.get_setting(cls._KEY_STOP_REASON, ""),
            "uptime_seconds": int((datetime.now(timezone.utc) - start_time).total_seconds())
            if start_time and running else 0,
        }


class TradingEngine:
    """
    Full ORB strategy execution engine.

    Orchestrates: market data → indicators → signal → risk → order → log
    """

    def __init__(
        self,
        client: Optional[UpstoxClient] = None,
        order_manager: Optional[OrderManager] = None,
        db_manager: Optional[DatabaseManager] = None,
        risk_manager: Optional[RiskManager] = None,
        position_sizer: Optional[PositionSizer] = None,
        exit_manager: Optional[ExitManager] = None,
        telegram_alerts: Optional[TelegramAlerts] = None,
        email_alerts: Optional[EmailAlerts] = None,
        strategy_name: str = "ORB_TREND_FOLLOWING",
    ) -> None:
        self.client = client or UpstoxClient()
        self.order_manager = order_manager or OrderManager(
            client=self.client,
            paper_mode=(settings.mode == "paper"),
        )
        self.db_manager = db_manager or DatabaseManager(db_path=settings.database.path)
        self.risk_manager = risk_manager or RiskManager(
            capital=settings.capital.total,
            daily_loss_limit=settings.risk.max_daily_loss_pct,
            max_trades_per_day=settings.risk.max_trades_per_day,
            max_concurrent_positions=settings.risk.max_concurrent_positions,
            max_consecutive_losses=settings.risk.max_consecutive_losses,
        )
        self.position_sizer = position_sizer or PositionSizer(
            capital=settings.capital.total,
            risk_per_trade=settings.risk.max_risk_per_trade_pct,
        )
        self.exit_manager = exit_manager or ExitManager(
            stop_loss_pct=settings.risk.max_risk_per_trade_pct,
        )
        self.telegram_alerts = telegram_alerts
        self.email_alerts = email_alerts
        self.strategy_name = strategy_name
        # Modular option-premium strategy registry.
        self.strategy_engine = MultiStrategyEngine()
        self.trailing_stop_manager = TrailingStopManager()
        # V21-FINAL Item 12: Daily floor trade DISABLED.
        # The bot must NOT trade merely because "there were zero trades today."
        # A zero-trade day is completely acceptable.
        self.enable_daily_floor_trade = False
        self.daily_floor_confidence = 60.0
        self.daily_floor_trigger_hour = 12
        self.daily_floor_trigger_minute = 0
        self._trades_taken_today = 0
        self._daily_floor_taken = False
        self._best_of_day: Optional[Dict[str, Any]] = None  # {"symbol":..., "confidence":...}
        self._open_positions: Dict[str, Dict[str, Any]] = {}
        # V21-FINAL: track whether critical live data is available
        self._option_data_stale = False
        self._position_mismatch: bool = False
        self._mismatch_reason: Optional[str] = None
        self._reconciled: bool = False
        try:
            self.hydrate_and_reconcile_positions()
        except Exception as e:
            logger.error("Initial position hydration error: %s", e)

    def update_access_token(self, token: str) -> None:
        """Update the shared Upstox client after OAuth token refresh."""
        if not token:
            raise ValueError("Access token cannot be empty")
        self.client.access_token = token
        if hasattr(self, "order_manager") and getattr(self.order_manager, "client", None) is not None:
            self.order_manager.client.access_token = token

    # ─── Position Hydration & Reconciliation ──────────────────────────────────

    def hydrate_and_reconcile_positions(self) -> Dict[str, Any]:
        """Startup position hydration & broker reconciliation sequence.

        START
        ↓
        Load active positions from SQLite via db_manager.get_open_positions()
        ↓
        Query actual Upstox positions via client.get_positions_with_details()
        ↓
        Compare local vs broker
        ↓
        Reconcile
        ↓
        Only then allow new trades
        """
        logger.info("Starting position hydration and reconciliation...")
        local_positions = self.db_manager.get_open_positions()
        is_live = settings.mode.lower() == "live"

        if not is_live:
            # Paper trading mode — SQLite state is authoritative
            self._open_positions.clear()
            for pos in local_positions:
                stop_loss = round(pos.average_price * (1.0 - settings.risk.max_risk_per_trade_pct), 2)
                target = round(pos.average_price * (1.0 + settings.risk.max_risk_per_trade_pct * 1.5), 2)
                self._open_positions[pos.symbol] = {
                    "trade_id": f"RECOVERED-{pos.symbol}",
                    "entry_price": pos.average_price,
                    "stop_loss": stop_loss,
                    "target": target,
                    "trailing_stop": stop_loss,
                    "strategy_name": self.strategy_name or "OPTION_PREMIUM",
                    "quantity": pos.quantity,
                    "requested_quantity": pos.quantity,
                    "side": pos.side,
                    "entry_time": pos.entry_time.isoformat() if hasattr(pos.entry_time, "isoformat") else str(pos.entry_time),
                    "contract_instrument_key": pos.symbol if "|" in pos.symbol else None,
                }
                if "|" in pos.symbol:
                    self._subscribe_option_contract(pos.symbol)
            self._position_mismatch = False
            self._mismatch_reason = None
            self._reconciled = True
            logger.info("Hydrated %d paper positions from SQLite", len(self._open_positions))
            return {
                "status": "reconciled",
                "mode": "paper",
                "positions_count": len(self._open_positions),
                "mismatch": False,
            }

        # Live trading mode — compare local vs broker
        try:
            broker_raw = self.client.get_positions_with_details()
            broker_open = [p for p in broker_raw if int(p.get("quantity", 0) or 0) != 0]
        except Exception as e:
            logger.error("Failed to query broker positions for reconciliation: %s", e)
            broker_open = []

        local_map: Dict[str, Position] = {p.symbol: p for p in local_positions}
        broker_map: Dict[str, Dict[str, Any]] = {}
        for bp in broker_open:
            key = bp.get("instrument_key") or bp.get("trading_symbol") or ""
            if key:
                broker_map[key] = bp

        mismatch_reasons = []

        # Check for broker positions missing locally
        for b_key, b_pos in broker_map.items():
            b_qty = int(b_pos.get("quantity", 0))
            matching_local = None
            for l_sym, l_pos in local_map.items():
                if l_sym == b_key or (b_pos.get("trading_symbol") and l_sym == b_pos.get("trading_symbol")):
                    matching_local = l_pos
                    break

            if matching_local is None:
                mismatch_reasons.append(
                    f"Broker position '{b_key}' (qty {b_qty}) missing locally in DB"
                )
            elif matching_local.quantity != b_qty:
                mismatch_reasons.append(
                    f"Quantity mismatch for '{b_key}': local={matching_local.quantity}, broker={b_qty}"
                )

        # Check for local positions missing at broker
        for l_sym, l_pos in local_map.items():
            matching_broker = None
            for b_key, b_pos in broker_map.items():
                if b_key == l_sym or (b_pos.get("trading_symbol") and b_pos.get("trading_symbol") == l_sym):
                    matching_broker = b_pos
                    break
            if matching_broker is None:
                mismatch_reasons.append(
                    f"Local position '{l_sym}' (qty {l_pos.quantity}) missing at broker"
                )

        if mismatch_reasons:
            self._position_mismatch = True
            self._mismatch_reason = "; ".join(mismatch_reasons)
            self._reconciled = False
            BotState.stop("POSITION MISMATCH: " + self._mismatch_reason)
            logger.critical("POSITION MISMATCH DETECTED: %s. Trading paused.", self._mismatch_reason)
            self.notify(f"🚨 POSITION MISMATCH: {self._mismatch_reason} — TRADING PAUSED")
            return {
                "status": "mismatch",
                "mode": "live",
                "mismatch": True,
                "reason": self._mismatch_reason,
            }

        # Positions match cleanly
        self._open_positions.clear()
        for pos in local_positions:
            stop_loss = round(pos.average_price * (1.0 - settings.risk.max_risk_per_trade_pct), 2)
            target = round(pos.average_price * (1.0 + settings.risk.max_risk_per_trade_pct * 1.5), 2)
            self._open_positions[pos.symbol] = {
                "trade_id": f"RECOVERED-{pos.symbol}",
                "entry_price": pos.average_price,
                "stop_loss": stop_loss,
                "target": target,
                "trailing_stop": stop_loss,
                "strategy_name": self.strategy_name or "OPTION_PREMIUM",
                "quantity": pos.quantity,
                "requested_quantity": pos.quantity,
                "side": pos.side,
                "entry_time": pos.entry_time.isoformat() if hasattr(pos.entry_time, "isoformat") else str(pos.entry_time),
                "contract_instrument_key": pos.symbol if "|" in pos.symbol else None,
            }
            if "|" in pos.symbol:
                self._subscribe_option_contract(pos.symbol)

        self._position_mismatch = False
        self._mismatch_reason = None
        self._reconciled = True
        logger.info("Successfully reconciled %d positions with broker", len(self._open_positions))
        return {
            "status": "reconciled",
            "mode": "live",
            "positions_count": len(self._open_positions),
            "mismatch": False,
        }

    # ─── Bot lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        BotState.start()
        logger.info("Trading engine started. Mode: %s", settings.mode)
        self.notify("🟢 Trading bot started. Mode: " + settings.mode.upper())

    def stop(self, reason: str = "Manual stop") -> None:
        BotState.stop(reason)
        logger.info("Trading engine stopped: %s", reason)
        self.notify(f"🔴 Trading bot stopped: {reason}")

    def kill(self, reason: str = "Emergency kill switch activated") -> None:
        BotState.kill(reason)
        self.notify(f"🚨 EMERGENCY KILL: {reason}")

    # ─── Market time checks ───────────────────────────────────────────────────

    @staticmethod
    def _is_market_open() -> bool:
        now = datetime.now(IST)
        if now.weekday() >= 5:
            return False
        open_t  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
        close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
        return open_t <= now <= close_t

    @staticmethod
    def _is_entry_window() -> bool:
        now = datetime.now(IST)
        entry_start = now.replace(hour=9,  minute=30, second=0, microsecond=0)
        entry_end   = now.replace(hour=12, minute=30, second=0, microsecond=0)
        return entry_start <= now <= entry_end

    @staticmethod
    def _is_exit_all_time() -> bool:
        now = datetime.now(IST)
        exit_t = now.replace(hour=14, minute=45, second=0, microsecond=0)
        return now >= exit_t

    # ─── ORB calculation ──────────────────────────────────────────────────────

    def _resolve_universe(self) -> tuple:
        """Resolve only index underlyings whose option premiums may be traded."""
        try:
            from backend.config.universe_config import load_universe_config
            uconfig = load_universe_config(self.db_manager)
            symbols = uconfig.resolve_symbols()
            if symbols:
                return symbols, uconfig.mode
        except Exception as e:
            logger.warning("Could not load option universe, using default: %s", e)
        return ["NIFTY50"], "OPTIONS"

    def detect_underlying_trend(self, symbol: str) -> str:
        """Real trend detection for an index/underlying, using the same
        EMA_TREND conditions used everywhere else — not a guess. Returns
        BULLISH / BEARISH / NEUTRAL. Used to auto-pick CE vs PE for options
        mode, since we're trading the index's option premium, not the
        index itself, so we still need to know its direction."""
        try:
            candles = self.client.get_historical_candles(symbol, "5minute", limit=100)
        except Exception as e:
            TradeLogger.log_error("TradingEngine.detect_underlying_trend", e, {"symbol": symbol})
            return "NEUTRAL"
        return classify_underlying_trend(candles)


    def evaluate_option_premium(
        self,
        underlying_symbol: str,
        expiry_date: Optional[str] = None,
        underlying_trend: Optional[str] = None,
    ) -> Any:
        """Run just the Option Premium strategy for `underlying_symbol`
        (e.g. 'NIFTY50', 'BANKNIFTY', 'SENSEX'). Fetches the real option
        chain, picks the ATM contract, fetches that contract's own candles,
        then scores momentum + VWAP. Returns a single StrategySignal
        (possibly NONE).

        `expiry_date` — if omitted, auto-picks the nearest real upcoming
        expiry from Upstox (never guesses a date).
        `underlying_trend` — if omitted, auto-detected via
        `detect_underlying_trend()` so this can run unattended from the
        scanner/live loop without a human picking a direction every time.
        """
        from backend.strategy.strategies.option_premium import OptionPremiumStrategy
        from backend.strategy.signal import StrategySignal

        if not expiry_date:
            try:
                expiry_date = self.client.get_nearest_expiry(underlying_symbol)
            except Exception as e:
                TradeLogger.log_error("TradingEngine.evaluate_option_premium", e,
                                       {"symbol": underlying_symbol})
                expiry_date = None
            if not expiry_date:
                sig = StrategySignal(strategy_name="OPTION_PREMIUM", symbol=underlying_symbol)
                sig.rejected_reasons = ["No upcoming option expiry found for this underlying"]
                sig.entry_reason = "NO TRADE — " + sig.rejected_reasons[0]
                return sig

        if not underlying_trend:
            underlying_trend = self.detect_underlying_trend(underlying_symbol)

        try:
            chain = self.client.get_option_chain(underlying_symbol, expiry_date)
        except Exception as e:
            TradeLogger.log_error("TradingEngine.evaluate_option_premium", e,
                                   {"symbol": underlying_symbol})
            chain = []

        spot = None
        try:
            spot_quotes = self.client.get_multiple_quotes([underlying_symbol])
            spot = spot_quotes.get(underlying_symbol, {}).get("ltp")
        except Exception:
            pass

        strat = next(
            (s for s in self.strategy_engine.strategies if s.name == "OPTION_PREMIUM"), None
        )
        if strat is None:
            strat = OptionPremiumStrategy()

        context = {
            "spot_price": spot,
            "underlying_trend": underlying_trend,
            "option_chain": chain,
            "expiry_date": expiry_date,
        }
        contract = strat.select_contract(context)
        if contract is None or not contract.get("instrument_key"):
            sig = StrategySignal(strategy_name="OPTION_PREMIUM", symbol=underlying_symbol)
            sig.rejected_reasons = ["Could not resolve ATM contract from live option chain/spot"]
            sig.entry_reason = "NO TRADE — " + sig.rejected_reasons[0]
            return sig

        try:
            premium_candles = self.client.get_historical_candles(
                contract["instrument_key"], "5minute", limit=30,
            )
        except Exception as e:
            TradeLogger.log_error("TradingEngine.evaluate_option_premium", e,
                                   {"symbol": contract["instrument_key"]})
            premium_candles = []

        return strat.evaluate(underlying_symbol, premium_candles, context)

    # ─── WebSocket subscription helpers (V21-FINAL Item 3) ────────────────────

    def _subscribe_option_contract(self, instrument_key: str) -> None:
        """Subscribe to live ticks for an option contract via the broker WS."""
        try:
            import backend.api.main as main_mod
            ws_client = getattr(getattr(main_mod, "app", None), "state", None)
            ws_client = getattr(ws_client, "ws_client", None)
            if ws_client is not None:
                ws_client.subscribe([instrument_key])
                logger.info("Subscribed to option contract WS feed: %s", instrument_key)
        except Exception as e:
            logger.warning("Could not subscribe to option contract %s: %s", instrument_key, e)

    def _unsubscribe_option_contract(self, instrument_key: str) -> None:
        """Unsubscribe from an option contract if no other position needs it."""
        # Check if any other open position uses this key
        other_uses = any(
            pos.get("contract_instrument_key") == instrument_key
            for sym, pos in self._open_positions.items()
        )
        if other_uses:
            return
        try:
            import backend.api.main as main_mod
            ws_client = getattr(getattr(main_mod, "app", None), "state", None)
            ws_client = getattr(ws_client, "ws_client", None)
            if ws_client is not None:
                ws_client.unsubscribe([instrument_key])
                logger.info("Unsubscribed from option contract WS feed: %s", instrument_key)
        except Exception as e:
            logger.warning("Could not unsubscribe from option contract %s: %s", instrument_key, e)

    def _get_option_tick_age(self, instrument_key: str) -> Optional[float]:
        """Get tick age for an option contract from the WS client."""
        try:
            import backend.api.main as main_mod
            ws_client = getattr(getattr(main_mod, "app", None), "state", None)
            ws_client = getattr(ws_client, "ws_client", None)
            if ws_client is not None:
                return ws_client.get_tick_age(instrument_key)
        except Exception:
            pass
        return None

    # ─── Main run loop ────────────────────────────────────────────────────────

    def execute_multi_signal(self, signal: Any) -> Optional[str]:
        """Execute an OPTION_PREMIUM `StrategySignal` through the full
        safety pipeline — risk checks, position sizing, order placement,
        position tracking.

        V21-FINAL: Orders now carry the actual option instrument_key.
        """
        if signal.signal != "BUY":
            return None

        if self._position_mismatch or not self._reconciled:
            logger.warning(
                "Trade blocked for %s: Position mismatch active (%s) or reconciliation pending",
                signal.symbol,
                self._mismatch_reason,
            )
            return None

        allowed, reason = self.risk_manager.can_take_trade(signal.symbol)
        if not allowed:
            TradeLogger.log_risk_event("BLOCKED", reason, signal.symbol)
            logger.info("Trade blocked for %s: %s", signal.symbol, reason)
            return None

        selected_contract = signal.indicators.get("selected_contract") if signal.indicators else None
        if not selected_contract:
            logger.warning("No broker-resolved option contract for %s — skipping", signal.symbol)
            return None

        contract_instrument_key = selected_contract.get("instrument_key")
        if not contract_instrument_key:
            logger.warning("No instrument_key in selected contract for %s — skipping", signal.symbol)
            return None

        lot_size = int(selected_contract.get("lot_size") or 0)
        freeze_limit = int(selected_contract.get("freeze_quantity") or 0)
        if lot_size <= 0 or freeze_limit <= 0:
            logger.warning("Missing broker lot/freeze metadata for %s — skipping", signal.symbol)
            return None

        # V21-FINAL Item 10: Lot-level risk check — BEFORE sizing
        lot_risk_ok, lot_risk_reason = self.risk_manager.check_lot_risk(
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            lot_size=lot_size,
        )
        if not lot_risk_ok:
            TradeLogger.log_risk_event("LOT_RISK_EXCEEDED", lot_risk_reason, signal.symbol)
            logger.info("Trade blocked for %s: %s", signal.symbol, lot_risk_reason)
            return None

        qty = self.position_sizer.calculate(
            entry_price=signal.entry_price,
            stop_loss_price=signal.stop_loss,
        )

        # Options trade in fixed lots, not arbitrary share counts. Round
        # down to the nearest whole lot (minimum 1 lot) — placing a
        # non-lot-multiple order would just get rejected by the exchange,
        # or worse, silently misprice the real risk being taken.
        qty = max(lot_size, (qty // lot_size) * lot_size)
        if qty > freeze_limit:
            capped_qty = (freeze_limit // lot_size) * lot_size
            logger.warning(
                "%s order size %d exceeds broker freeze limit %d — capping to %d",
                signal.symbol, qty, freeze_limit, capped_qty,
            )
            qty = max(lot_size, capped_qty)

        if qty <= 0:
            logger.warning("Quantity 0 for %s — skipping", signal.symbol)
            return None

        # V21-FINAL Item 11: Exposure check
        exposure_ok, exposure_reason = self.risk_manager.check_exposure(
            new_entry_price=signal.entry_price,
            new_quantity=qty,
        )
        if not exposure_ok:
            TradeLogger.log_risk_event("EXPOSURE_EXCEEDED", exposure_reason, signal.symbol)
            logger.info("Trade blocked for %s: %s", signal.symbol, exposure_reason)
            return None

        # V21-FINAL Item 2: Contract consistency validation
        contract_metadata = {
            "option_type": selected_contract.get("option_type"),
            "strike": selected_contract.get("strike"),
            "expiry": selected_contract.get("expiry") or (
                signal.indicators.get("expiry_date") if signal.indicators else None
            ),
            "lot_size": lot_size,
            "freeze_quantity": freeze_limit,
        }

        trade_id = str(uuid.uuid4())
        try:
            # V21-FINAL Item 1: OrderRequest carries instrument_key
            req = OrderRequest(
                symbol=signal.symbol,
                instrument_key=contract_instrument_key,
                side="BUY",
                quantity=qty,
                price=signal.entry_price,
                order_type="MARKET",
                contract_metadata=contract_metadata,
                underlying_symbol=signal.symbol,
            )
            order = self.order_manager.place_order(req)

            # V21-FINAL Items 15/16: Don't create filled position if order not filled
            if order.status in (OrderStatus.REJECTED, OrderStatus.FAILED):
                logger.warning("Order rejected/failed for %s: %s", signal.symbol, order.fill_details)
                return None

            actual_qty = order.filled_quantity or order.quantity or qty
            actual_price = order.average_fill_price or order.price or signal.entry_price

            # V21-FINAL Item 3: Subscribe to option contract for live ticks
            self._subscribe_option_contract(contract_instrument_key)

            trade = Trade(
                id=trade_id, symbol=signal.symbol, side="long", quantity=actual_qty,
                price=actual_price,
                timestamp=datetime.now(timezone.utc), strategy=signal.strategy_name,
                status="filled", pnl=None,
                notes=f"setup_score={signal.confidence:.1f} strategy={signal.strategy_name} "
                      f"instrument={contract_instrument_key}",
            )
            self.db_manager.insert_trade(trade)

            self._open_positions[signal.symbol] = {
                "trade_id": trade_id,
                "entry_price": actual_price,
                "stop_loss": signal.stop_loss,
                "target": signal.target,
                "trailing_stop": signal.stop_loss,
                "strategy_name": signal.strategy_name,
                "quantity": actual_qty,
                "requested_quantity": qty,
                "atr": signal.indicators.get("atr", 0.0),
                "side": "long",
                "entry_time": datetime.now(timezone.utc).isoformat(),
                # For OPTION_PREMIUM positions: the actual contract being
                # held. Exit monitoring MUST watch this contract's own
                # price, not the underlying index's — a NIFTY option's
                # stop-loss is in premium terms (e.g. ₹140), which has no
                # relationship to the index's spot price (e.g. 22,150).
                "contract_instrument_key": contract_instrument_key,
                "contract_info": {
                    "option_type": selected_contract.get("option_type"),
                    "strike": selected_contract.get("strike"),
                    "lot_size": lot_size,
                },
                "expiry_date": signal.indicators.get("expiry_date") if signal.indicators else None,
                "order_id": order.id,
                "fill_details": order.fill_details,
            }
            self.db_manager.upsert_position(Position(
                symbol=signal.symbol, quantity=actual_qty,
                average_price=actual_price,
                entry_time=datetime.now(timezone.utc), side="long", unrealized_pnl=0.0,
            ))

            self.risk_manager.record_trade_opened()
            self.risk_manager.record_exposure_opened(actual_price * actual_qty)

            TradeLogger.log_entry(
                trade_id, signal.symbol, "BUY", actual_qty,
                actual_price, signal.stop_loss,
                signal.indicators.get("atr", 0.0), signal.indicators.get("rsi", 0.0),
                0.0, signal.indicators.get("volume_ratio", 0.0),
                signal.indicators.get("orb_high", 0.0), signal.indicators.get("orb_low", 0.0),
                "BULLISH", settings.mode, signal.conditions,
            )

            msg = (
                f"{'📝' if settings.mode == 'paper' else '🟢'} ENTRY: {signal.symbol} "
                f"({selected_contract.get('option_type')} {selected_contract.get('strike')}) "
                f"@ ₹{actual_price:.2f} | SL: ₹{signal.stop_loss:.2f} | Target: ₹{signal.target:.2f} | "
                f"Qty: {actual_qty} | {signal.strategy_name} ({signal.confidence:.0f}% setup score) | "
                f"Instrument: {contract_instrument_key}"
            )
            self.notify(msg)
            return trade_id

        except OrderError as e:
            TradeLogger.log_error("TradingEngine.execute_multi_signal", e, {"symbol": signal.symbol})
            logger.error("Order failed for %s: %s", signal.symbol, e)
            return None

    async def _monitor_open_positions(self) -> None:
        """Check every open position against its stop-loss, target, and
        trailing stop on every loop iteration.

        V21-FINAL: Uses live WS ticks for option contracts. Falls back to
        broker quote if tick is stale. Never silently uses historical candle
        close as a real-time price.
        """
        try:
            from backend.api.websocket import get_prices_by_symbol
            live_prices = get_prices_by_symbol()
        except Exception:
            live_prices = {}

        for symbol in list(self._open_positions.keys()):
            pos = self._open_positions[symbol]
            # For OPTION_PREMIUM positions, everything below must watch the
            # actual contract's price — comparing an option's premium-based
            # stop-loss against the underlying index's spot price would be
            # comparing two unrelated numbers.
            contract_key = pos.get("contract_instrument_key")
            price_lookup_key = contract_key or symbol

            tick = live_prices.get(price_lookup_key)
            current_price = tick.get("ltp") if tick else None

            # V21-FINAL Item 4: Check tick freshness for option contracts
            if contract_key:
                tick_age = self._get_option_tick_age(contract_key)
                if tick_age is not None and tick_age > MAX_OPTION_TICK_AGE_SECONDS:
                    # Tick is stale — try emergency broker quote
                    logger.warning(
                        "Option tick for %s is stale (%.1fs old) — attempting fresh broker quote",
                        contract_key, tick_age,
                    )
                    try:
                        fresh_quote = self.client.get_quote_by_instrument_key(contract_key)
                        if fresh_quote.get("has_data") and fresh_quote.get("ltp", 0) > 0:
                            current_price = fresh_quote["ltp"]
                        else:
                            current_price = None
                    except Exception:
                        current_price = None

                    if not current_price:
                        # Mark as stale — do NOT use historical candle as substitute
                        self._option_data_stale = True
                        logger.warning(
                            "STALE OPTION DATA for %s — no live tick or broker quote. "
                            "Position monitoring degraded. SAFETY ALERT.",
                            contract_key,
                        )
                        continue  # skip this position's SL/target check — don't act on stale data

                elif current_price is None and tick is None:
                    # No tick at all — try broker quote
                    try:
                        fresh_quote = self.client.get_quote_by_instrument_key(contract_key)
                        if fresh_quote.get("has_data") and fresh_quote.get("ltp", 0) > 0:
                            current_price = fresh_quote["ltp"]
                    except Exception:
                        pass

            elif not current_price:
                # No live tick yet for a regular stock/index — fall back to
                # a fresh quote so a dead WebSocket feed doesn't leave
                # positions unmonitored.
                try:
                    quotes = self.client.get_multiple_quotes([symbol])
                    current_price = quotes.get(symbol, {}).get("ltp")
                except Exception:
                    current_price = None

            if not current_price:
                continue  # genuinely no price available — don't guess

            trail = self.trailing_stop_manager.compute(
                entry_price=pos["entry_price"], initial_stop=pos["stop_loss"],
                current_price=current_price, current_stop=pos.get("trailing_stop", pos["stop_loss"]),
            )
            pos["trailing_stop"] = trail["stop"]

            exit_reason: Optional[str] = None
            if current_price <= pos["trailing_stop"]:
                # Same fix as the backtest engine: label from whether the
                # stop actually moved, not from this bar's freshly-
                # recomputed stage (which forgets earlier ratcheting).
                exit_reason = "TRAILING_STOP_HIT" if pos["trailing_stop"] > pos["stop_loss"] else "STOP_LOSS_HIT"
            elif pos.get("target", 0) > 0 and current_price >= pos["target"]:
                exit_reason = "TARGET_HIT"
            else:
                try:
                    candles = self.client.get_historical_candles(price_lookup_key, "5minute", limit=100)
                    exit_context = {"expiry_date": pos.get("expiry_date")} if pos.get("expiry_date") else None
                    strat_exit = self.strategy_engine.check_exits(
                        pos.get("strategy_name", self.strategy_name), pos, candles, exit_context,
                    )
                    if strat_exit:
                        exit_reason = strat_exit
                except Exception as e:
                    logger.debug("Strategy exit check failed for %s: %s", symbol, e)

            if exit_reason:
                await self._close_position(symbol, exit_reason)

    def _maybe_take_daily_floor_trade(self, now: datetime) -> Optional[str]:
        """V21-FINAL Item 12: DISABLED. The bot must NOT trade merely
        because "there were zero trades today." A zero-trade day is
        completely acceptable. No valid setup → 0 trades.

        This method is preserved for reference but always returns None.
        """
        # DISABLED — enable_daily_floor_trade is always False
        if not self.enable_daily_floor_trade:
            return None

        if not (
            not self._daily_floor_taken
            and self._trades_taken_today == 0
            and self._best_of_day is not None
            and now.hour == self.daily_floor_trigger_hour
            and now.minute >= self.daily_floor_trigger_minute
        ):
            return None

        self._daily_floor_taken = True  # only ever attempt once per day
        candidate_symbol = self._best_of_day["symbol"]
        fresh_best = self.evaluate_option_premium(candidate_symbol)

        if fresh_best is None or fresh_best.confidence < self.daily_floor_confidence or fresh_best.entry_price <= 0:
            return None

        fresh_best.signal = "BUY"
        trade_id = self.execute_multi_signal(fresh_best)
        if trade_id:
            self._trades_taken_today += 1
        return trade_id

    async def run_forever(self, poll_interval_seconds: float = 10.0) -> None:
        """Wrapper around `run_trading_session()` for a long-lived background
        task (whether that's this same web process or a standalone worker).

        `run_trading_session()`'s own loop condition is `while
        BotState.is_running():` — if the bot isn't started yet (the normal
        state right after boot, before anyone's clicked Start), it returns
        almost immediately, having done nothing. Calling it once, directly,
        as the background task would mean the task quietly finishes at
        boot and NEVER comes back even after the user clicks Start later.
        This wrapper polls BotState and re-enters `run_trading_session()`
        every time it becomes active, for as long as the process runs.
        """
        logger.info("Trading supervisor started — waiting for Start via the dashboard")
        while True:
            if BotState.is_running():
                logger.info("BotState is running — entering trading session")
                try:
                    await self.run_trading_session()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.exception("Trading session crashed, retrying after backoff: %s", e)
                    await asyncio.sleep(30)
                logger.info("Trading session ended — back to waiting for Start")
            else:
                await asyncio.sleep(poll_interval_seconds)

    async def run_trading_session(self) -> None:
        """Main async trading loop — runs during market hours.

        V21-FINAL: daily floor trade removed from the loop.
        """
        logger.info("Trading session starting")

        while BotState.is_running():
            try:
                now = datetime.now(IST)
                watchlist, universe_mode = self._resolve_universe()

                # Monitor every open position EVERY cycle — the critical fix.
                if self._open_positions:
                    await self._monitor_open_positions()

                # Force exit all at 14:45
                if self._is_exit_all_time() and self._open_positions:
                    logger.info("14:45 — forcing exit of all positions")
                    for sym in list(self._open_positions.keys()):
                        await self._close_position(sym, "TIME_FORCE_EXIT")

                # Main entry loop
                if self._is_entry_window() and self._is_market_open():
                    if self._position_mismatch or not self._reconciled:
                        logger.warning(
                            "Skipping entries — POSITION MISMATCH ACTIVE: %s",
                            self._mismatch_reason or "Reconciliation pending",
                        )
                    # V21-FINAL Item 4: Block new entries while critical data is stale
                    elif self._option_data_stale:
                        logger.warning("Skipping entries — option data marked STALE")
                    else:
                        for sym in watchlist:
                            if not BotState.is_running():
                                break
                            if sym in self._open_positions:
                                continue

                            best = self.evaluate_option_premium(sym)

                            if best is not None and best.signal == "BUY":
                                trade_id = self.execute_multi_signal(best)
                                if trade_id:
                                    self._trades_taken_today += 1

                    # V21-FINAL Item 12: Daily floor trade REMOVED from loop
                    # self._maybe_take_daily_floor_trade(now)  # DISABLED

                # Reset daily state at end of day
                if now.hour == 15 and now.minute >= 30:
                    self.risk_manager.reset_for_new_day()
                    self._trades_taken_today = 0
                    self._daily_floor_taken = False
                    self._best_of_day = None
                    self._option_data_stale = False

                # Sleep in short chunks rather than one 300s block, so a
                # Stop/Kill from the dashboard takes effect within a few
                # seconds instead of up to 5 minutes — the loop condition
                # is only re-checked once per full sleep otherwise.
                for _ in range(60):  # 60 x 5s = 300s total, same overall cadence
                    if not BotState.is_running():
                        break
                    await asyncio.sleep(5)

            except Exception as e:
                TradeLogger.log_error("TradingEngine.run_trading_session", e)
                logger.error("Trading loop error: %s", e)
                await asyncio.sleep(30)

    async def _close_position(self, symbol: str, reason: str) -> None:
        """Close an open position.

        V21-FINAL Item 17: Exit order uses the actual option contract
        instrument_key, NOT the underlying symbol.
        """
        pos = self._open_positions.get(symbol)
        if not pos:
            return
        try:
            contract_key = pos.get("contract_instrument_key")
            contract_metadata = None
            if contract_key and pos.get("contract_info"):
                info = pos["contract_info"]
                contract_metadata = {
                    "option_type": info.get("option_type"),
                    "strike": info.get("strike"),
                    "expiry": pos.get("expiry_date"),
                    "lot_size": info.get("lot_size"),
                    "freeze_quantity": info.get("freeze_quantity"),
                }

            # V21-FINAL: Exit uses the same instrument_key as entry
            req = OrderRequest(
                symbol=symbol,
                instrument_key=contract_key,
                side="SELL",
                quantity=pos["quantity"],
                order_type="MARKET",
                contract_metadata=contract_metadata,
                underlying_symbol=symbol,
            )
            order = self.order_manager.place_order(req)

            # V21-FINAL Item 18: Use actual fill price for P&L
            actual_exit_qty = order.filled_quantity or order.quantity or pos["quantity"]
            exit_price = order.average_fill_price or order.price or pos["entry_price"]

            gross_pnl = (exit_price - pos["entry_price"]) * actual_exit_qty
            brokerage = (pos["entry_price"] + exit_price) * actual_exit_qty * 0.0003
            stt = exit_price * actual_exit_qty * 0.0015  # options STT
            net_pnl = gross_pnl - brokerage - stt
            pnl_r = gross_pnl / ((pos["entry_price"] - pos["stop_loss"]) * actual_exit_qty) if pos["stop_loss"] else 0

            self.risk_manager.record_trade_result(net_pnl)
            self.risk_manager.record_exposure_closed(pos["entry_price"] * actual_exit_qty)
            self.db_manager.delete_position(symbol)

            # V21-FINAL Item 3: Unsubscribe from option contract
            if contract_key:
                del self._open_positions[symbol]
                self._unsubscribe_option_contract(contract_key)
            else:
                del self._open_positions[symbol]

            TradeLogger.log_exit(
                pos["trade_id"], symbol, exit_price, reason,
                gross_pnl, net_pnl, pnl_r, 0, 1, settings.mode,
            )
            pnl_icon = "✅" if net_pnl >= 0 else "❌"
            self.notify(
                f"{pnl_icon} EXIT: {symbol} @ ₹{exit_price:.2f} | "
                f"PnL: ₹{net_pnl:.0f} (gross: ₹{gross_pnl:.0f}, charges: ₹{brokerage + stt:.0f}) "
                f"({reason}) | Instrument: {contract_key or 'N/A'}"
            )

        except Exception as e:
            TradeLogger.log_error("TradingEngine._close_position", e, {"symbol": symbol})

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def notify(self, message: str) -> None:
        for notifier in [self.telegram_alerts, self.email_alerts]:
            if notifier is None:
                continue
            try:
                if isinstance(notifier, TelegramAlerts):
                    notifier.send_message(message)
                elif isinstance(notifier, EmailAlerts):
                    notifier.send_email("Upstox Bot Alert", message)
            except Exception:
                pass

    # ─── Legacy compat ────────────────────────────────────────────────────────

    def list_trades(self):
        return self.db_manager.list_trades()

    def list_positions(self):
        return self.db_manager.list_positions()

    def get_open_positions_detail(self) -> List[Dict[str, Any]]:
        """Everything the dashboard needs, per open position.

        V21-FINAL Item 28: Now includes contract info, instrument key,
        expiry, bid/ask, spread, tick age, and data status.
        """
        try:
            from backend.api.websocket import get_prices_by_symbol
            live_prices = get_prices_by_symbol()
        except Exception:
            live_prices = {}

        details: List[Dict[str, Any]] = []
        for symbol, pos in self._open_positions.items():
            contract_key = pos.get("contract_instrument_key")
            tick = live_prices.get(contract_key or symbol)
            current_price = tick.get("ltp") if tick else None
            bid_price = None
            ask_price = None
            tick_age = None
            data_status = "UNKNOWN"

            if tick:
                tick_mono = tick.get("last_tick_monotonic")
                if tick_mono:
                    tick_age = round(time.monotonic() - tick_mono, 1)
                    data_status = "LIVE" if tick_age < MAX_OPTION_TICK_AGE_SECONDS else "STALE"

            if not current_price and contract_key:
                try:
                    fresh_quote = self.client.get_quote_by_instrument_key(contract_key)
                    if fresh_quote.get("has_data"):
                        current_price = fresh_quote.get("ltp")
                        bid_price = fresh_quote.get("bid_price")
                        ask_price = fresh_quote.get("ask_price")
                        data_status = "QUOTE_FALLBACK"
                except Exception:
                    data_status = "STALE"

            trailing_stop = pos.get("trailing_stop", pos["stop_loss"])
            if current_price:
                trail = self.trailing_stop_manager.compute(
                    entry_price=pos["entry_price"],
                    initial_stop=pos["stop_loss"],
                    current_price=current_price,
                    current_stop=trailing_stop,
                )
                trailing_stop = trail["stop"]
                pos["trailing_stop"] = trailing_stop  # persist the ratchet

            current_pnl = (
                round((current_price - pos["entry_price"]) * pos["quantity"], 2)
                if current_price else None
            )
            current_pnl_pct = (
                round((current_price - pos["entry_price"]) / pos["entry_price"] * 100, 2)
                if current_price else None
            )

            # Calculate spread
            spread_pct = None
            if bid_price and ask_price and bid_price > 0:
                spread_pct = round((ask_price - bid_price) / bid_price * 100, 2)

            # Risk amount for this position
            risk_amount = round(
                abs(pos["entry_price"] - pos["stop_loss"]) * pos["quantity"], 2
            ) if pos.get("stop_loss") else None

            contract_info = pos.get("contract_info", {})

            details.append({
                "symbol": symbol,
                "strategy_used": pos.get("strategy_name", self.strategy_name),
                "entry_price": pos["entry_price"],
                "target": pos.get("target", 0.0),
                "stop_loss": pos["stop_loss"],
                "trailing_stop": trailing_stop,
                "quantity": pos["quantity"],
                "current_price": current_price,
                "current_pnl": current_pnl,
                "current_pnl_pct": current_pnl_pct,
                "mode": settings.mode,
                "entry_time": pos.get("entry_time"),
                # V21-FINAL Item 28: contract and data freshness info
                "underlying": symbol,
                "contract_name": (
                    f"{symbol} {contract_info.get('strike', '')} {contract_info.get('option_type', '')}"
                    if contract_info else symbol
                ),
                "instrument_key": contract_key,
                "expiry": pos.get("expiry_date"),
                "bid": bid_price,
                "ask": ask_price,
                "spread_pct": spread_pct,
                "tick_age_seconds": tick_age,
                "data_status": data_status,
                "risk_amount": risk_amount,
            })
        return details

    def should_exit(self, position: dict, current_price: float) -> bool:
        return self.exit_manager.should_exit(position, current_price)
