"""Production trading engine — full ORB strategy with safety controls."""
from __future__ import annotations

import asyncio
import logging
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
from backend.orders.order_models import OrderRequest
from backend.risk.position_sizer import PositionSizer
from backend.risk.risk_manager import RiskManager
from backend.strategy.exit_manager import ExitManager, TrailingStopManager
from backend.strategy.strategy_engine import MultiStrategyEngine

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
settings = load_settings()

# NSE/BSE F&O lot sizes for the indices this bot supports in OPTIONS mode.
# These change periodically when exchanges revise them — verify against
# the current NSE/BSE circular before relying on this for real orders.
LOT_SIZES: Dict[str, int] = {
    "NIFTY50": 75,
    "BANKNIFTY": 30,
    "SENSEX": 20,
}

# Exchange "quantity freeze" limits — the maximum quantity allowed in a
# SINGLE order for index derivatives, meant to stop fat-finger orders from
# moving the market. An order above this is rejected outright by the
# exchange, not just flagged. These are revised periodically (NIFTY has
# changed at least twice in the past year) — verify against the current
# NSE/BSE circular. This bot caps order size at the limit rather than
# splitting into multiple orders, so a signal sized above the freeze limit
# trades AT the limit, not the full computed size.
FREEZE_QUANTITY_LIMITS: Dict[str, int] = {
    "NIFTY50": 1800,
    "BANKNIFTY": 600,
    "SENSEX": 900,   # not confirmed from BSE's own published limit — verify before live use
}


@dataclass
class SignalResult:
    """Result of strategy evaluation for one symbol."""
    symbol: str
    signal: str           # "long" | "skip"
    confidence: float     # 0.0 to 1.0
    conditions: Dict[str, bool] = field(default_factory=dict)
    skip_reason: str = ""
    entry_price: float = 0.0
    stop_loss: float = 0.0
    atr: float = 0.0
    rsi: float = 0.0
    choppiness: float = 0.0
    volume_ratio: float = 0.0
    orb_high: float = 0.0
    orb_low: float = 0.0
    trend_bias: str = "NEUTRAL"


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

    NIFTY50 = [
        "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","HINDUNILVR","KOTAKBANK",
        "LT","SBIN","AXISBANK","BHARTIARTL","ITC","ASIANPAINT","MARUTI","HCLTECH",
        "SUNPHARMA","WIPRO","TITAN","ULTRACEMCO","BAJFINANCE",
    ]

    def __init__(
        self,
        order_manager: Optional[OrderManager] = None,
        db_manager: Optional[DatabaseManager] = None,
        risk_manager: Optional[RiskManager] = None,
        position_sizer: Optional[PositionSizer] = None,
        exit_manager: Optional[ExitManager] = None,
        telegram_alerts: Optional[TelegramAlerts] = None,
        email_alerts: Optional[EmailAlerts] = None,
        strategy_name: str = "ORB_TREND_FOLLOWING",
    ) -> None:
        self.client = UpstoxClient()
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
        # Pluggable multi-strategy engine (EMA Trend / ORB / Option Premium).
        # This runs alongside the legacy inline ORB evaluate_signal() below —
        # see evaluate_all_strategies() for the new, unified path.
        self.strategy_engine = MultiStrategyEngine()
        self.trailing_stop_manager = TrailingStopManager()
        # "At least one good trade per day" — see run_trading_session(). This
        # is a RELAXED floor, not an indiscriminate force-trade: it only
        # fires on the single best candidate seen all day, re-checked fresh
        # right before taking it, and it still respects every risk limit.
        self.enable_daily_floor_trade = True
        self.daily_floor_confidence = 60.0
        self.daily_floor_trigger_hour = 12
        self.daily_floor_trigger_minute = 0
        self._trades_taken_today = 0
        self._daily_floor_taken = False
        self._best_of_day: Optional[Dict[str, Any]] = None  # {"symbol":..., "confidence":...}
        self._orb_levels: Dict[str, Dict[str, float]] = {}
        self._open_positions: Dict[str, Dict[str, Any]] = {}

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

    def _resolve_watchlist(self) -> List[str]:
        """What the live loop actually scans — driven by the real, saved
        UniverseConfig (item #4), not a hardcoded slice. Falls back to the
        old hardcoded NIFTY50 slice only if the universe config can't be
        loaded for some reason, so the bot never just stops scanning."""
        symbols, _mode = self._resolve_universe()
        return symbols

    def _resolve_universe(self) -> tuple:
        """(symbols, mode) — mode is 'STOCKS' or 'OPTIONS'. OPTIONS mode
        means `symbols` are underlying indices (e.g. NIFTY50, SENSEX)
        whose OPTION PREMIUMS get traded via OptionPremiumStrategy, not
        their own index price action."""
        try:
            from backend.config.universe_config import load_universe_config
            uconfig = load_universe_config(self.db_manager)
            symbols = uconfig.resolve_symbols()
            if symbols:
                return symbols, uconfig.mode
        except Exception as e:
            logger.warning("Could not load universe config, using default watchlist: %s", e)
        return self.NIFTY50[: settings.universe.max_stocks_in_watchlist], "STOCKS"

    def _capture_orb(self, symbol: str) -> Optional[Dict[str, float]]:
        """Fetch opening range (9:15–9:30 candles) for symbol."""
        try:
            today = date.today().strftime("%Y-%m-%d")
            candles = self.client.get_historical_candles(symbol, "1minute", limit=30)
            if not candles:
                return None
            orb_candles = [
                c for c in candles
                if c.get("timestamp", "")[:16] >= f"{today}T09:15"
                and c.get("timestamp", "")[:16] <= f"{today}T09:29"
            ]
            if not orb_candles:
                return None
            orb_high = max(c["high"] for c in orb_candles)
            orb_low  = min(c["low"]  for c in orb_candles)
            width    = orb_high - orb_low
            logger.debug("ORB %s: H=%.2f L=%.2f W=%.2f", symbol, orb_high, orb_low, width)
            return {"orb_high": orb_high, "orb_low": orb_low, "orb_width": width}
        except Exception as e:
            TradeLogger.log_error("TradingEngine._capture_orb", e, {"symbol": symbol})
            return None

    # ─── Signal evaluation ────────────────────────────────────────────────────

    def evaluate_signal(self, symbol: str) -> SignalResult:
        """
        Full ORB signal evaluation with confidence scoring.

        Conditions checked (each worth 1 point):
        1. Trend bias BULLISH (15-min EMA alignment)
        2. Price above ORB High
        3. Candle body >= 60% of range
        4. Volume ratio >= 1.5x
        5. RSI between 55–75
        6. Choppiness Index < 61.8
        7. ATR not in bottom 20th percentile
        8. ORB width in valid range
        9. No existing open position
        10. Within entry time window

        Confidence = passed_conditions / total_conditions
        Minimum confidence to trade: 0.8 (8/10 conditions)
        """
        result = SignalResult(symbol=symbol, signal="skip", confidence=0.0)

        try:
            candles = self.client.get_historical_candles(symbol, "5minute", limit=100)
            if not candles or len(candles) < 30:
                result.skip_reason = "Insufficient candle data"
                return result

            closes  = [c["close"]  for c in candles]
            highs   = [c["high"]   for c in candles]
            lows    = [c["low"]    for c in candles]
            volumes = [c["volume"] for c in candles]
            latest  = candles[-1]

            # Indicators
            ema20 = calculate_ema(closes, 20)
            ema50 = calculate_ema(closes, 50)
            ema200= calculate_ema(closes, 200) if len(closes) >= 200 else []
            rsi_vals = calculate_rsi(closes, 14)
            atr_vals = calculate_atr(highs, lows, closes, 14)
            ci_vals  = choppiness_index(highs, lows, closes, 14)

            current_rsi = rsi_vals[-1] if rsi_vals else 50
            current_atr = atr_vals[-1] if atr_vals else 0
            current_ci  = ci_vals[-1]  if ci_vals  else 50

            vol_avg = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 1
            vol_ratio = volumes[-1] / vol_avg if vol_avg > 0 else 0

            # ATR percentile rank
            atr_pct = 0.5
            if len(atr_vals) >= 50:
                atr_window = atr_vals[-50:]
                atr_min, atr_max = min(atr_window), max(atr_window)
                if atr_max > atr_min:
                    atr_pct = (current_atr - atr_min) / (atr_max - atr_min)

            # ORB levels
            orb = self._orb_levels.get(symbol)
            if not orb:
                result.skip_reason = "ORB not captured yet"
                return result

            orb_high  = orb["orb_high"]
            orb_low   = orb["orb_low"]
            orb_width = orb["orb_width"]

            # Trend bias
            trend_bullish = False
            if len(ema20) > 0 and len(ema50) > 0:
                trend_bullish = (
                    ema20[-1] > ema50[-1]
                    and closes[-1] > ema20[-1]
                    and (not ema200 or closes[-1] > ema200[-1])
                )

            # Candle body strength
            body = abs(latest["close"] - latest["open"])
            candle_range = latest["high"] - latest["low"]
            body_pct = (body / candle_range) if candle_range > 0 else 0

            # Conditions
            cond: Dict[str, bool] = {
                "trend_bullish":         trend_bullish,
                "price_above_orb":       latest["close"] > orb_high,
                "strong_body":           body_pct >= 0.60,
                "volume_ok":             vol_ratio >= 1.5,
                "rsi_in_range":          55 <= current_rsi <= 75,
                "not_choppy":            current_ci < 61.8,
                "atr_not_low":           atr_pct > 0.20,
                "orb_width_valid":       0.3 * current_atr <= orb_width <= 2.5 * current_atr if current_atr > 0 else False,
                "no_existing_position":  symbol not in self._open_positions,
                "entry_window":          self._is_entry_window(),
            }

            passed = sum(1 for v in cond.values() if v)
            confidence = passed / len(cond)

            result.conditions   = cond
            result.confidence   = round(confidence, 3)
            result.rsi          = round(current_rsi, 2)
            result.atr          = round(current_atr, 4)
            result.choppiness   = round(current_ci, 2)
            result.volume_ratio = round(vol_ratio, 2)
            result.orb_high     = orb_high
            result.orb_low      = orb_low
            result.trend_bias   = "BULLISH" if trend_bullish else "NEUTRAL"

            if confidence >= 0.80:
                result.signal      = "long"
                result.entry_price = latest["close"]
                result.stop_loss   = latest["close"] - (1.5 * current_atr)
            else:
                failing = [k for k, v in cond.items() if not v]
                result.skip_reason = "Conditions not met: " + ", ".join(failing[:3])

            TradeLogger.log_signal(
                symbol, result.signal, confidence,
                [f"{k}={'✓' if v else '✗'}" for k, v in cond.items()],
                skipped=result.signal == "skip",
                skip_reason=result.skip_reason,
            )
            TradeLogger.log_condition(symbol, cond)

        except Exception as e:
            TradeLogger.log_error("TradingEngine.evaluate_signal", e, {"symbol": symbol})
            result.skip_reason = str(e)

        return result

    # ─── Multi-strategy evaluation (EMA Trend / ORB / Option Premium) ────────

    def evaluate_all_strategies(
        self, symbol: str, index_trend: Optional[str] = None,
    ) -> List[Any]:
        """Run every enabled strategy (EMA_TREND, ORB, OPTION_PREMIUM) against
        `symbol` and return one StrategySignal per strategy — including
        rejected ones with their reasons. This is the path the live scanner
        and dashboard use; it never hides a rejection.

        `index_trend` — optional "BULLISH"/"BEARISH"/"NEUTRAL" for the
        broader index, used by ORB's index-trend-confirmation condition.
        Option Premium strategy is skipped here (it needs an option chain +
        underlying trend context that only makes sense for index/F&O
        symbols) unless the caller supplies that separately.
        """
        try:
            candles = self.client.get_historical_candles(symbol, "5minute", limit=100)
        except Exception as e:
            TradeLogger.log_error("TradingEngine.evaluate_all_strategies", e, {"symbol": symbol})
            candles = []

        context: Dict[str, Any] = {}
        if index_trend:
            context["index_trend"] = index_trend

        strategy_names = ["EMA_TREND", "ORB"]
        return self.strategy_engine.evaluate(symbol, candles, context, strategy_names)

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

        ema_strat = next(
            (s for s in self.strategy_engine.strategies if s.name == "EMA_TREND"), None
        )
        if ema_strat is None or len(candles) < ema_strat.min_candles:
            return "NEUTRAL"
        sig = ema_strat.evaluate(symbol, candles)
        if sig.conditions.get("ema_trend_up") and sig.conditions.get("price_above_ema20"):
            return "BULLISH"
        if sig.conditions.get("ema_trend_up") is False:
            return "BEARISH"
        return "NEUTRAL"

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

    # ─── Order execution ──────────────────────────────────────────────────────

    def execute_signal(self, signal: SignalResult) -> Optional[str]:
        """
        Execute a validated signal through the full safety pipeline.
        Returns trade_id on success, None on skip/failure.
        """
        if signal.signal != "long":
            return None

        allowed, reason = self.risk_manager.can_take_trade(signal.symbol)
        if not allowed:
            TradeLogger.log_risk_event("BLOCKED", reason, signal.symbol)
            logger.info("Trade blocked for %s: %s", signal.symbol, reason)
            return None

        qty = self.position_sizer.calculate(
            entry_price=signal.entry_price,
            stop_loss_price=signal.stop_loss,
        )
        if qty <= 0:
            logger.warning("Quantity 0 for %s — skipping", signal.symbol)
            return None

        trade_id = str(uuid.uuid4())
        try:
            req = OrderRequest(
                symbol=signal.symbol,
                side="BUY",
                quantity=qty,
                price=signal.entry_price,
                order_type="MARKET",
            )
            order = self.order_manager.place_order(req)

            # Record trade
            trade = Trade(
                id=trade_id,
                symbol=signal.symbol,
                side="long",
                quantity=qty,
                price=order.price or signal.entry_price,
                timestamp=datetime.now(timezone.utc),
                strategy=self.strategy_name,
                status="filled",
                pnl=None,
                notes=f"confidence={signal.confidence:.2f} atr={signal.atr:.2f}",
            )
            self.db_manager.insert_trade(trade)

            # Track position
            self._open_positions[signal.symbol] = {
                "trade_id": trade_id,
                "entry_price": order.price or signal.entry_price,
                "stop_loss": signal.stop_loss,
                "target": getattr(signal, "target", 0.0),
                "trailing_stop": signal.stop_loss,  # starts at the initial stop; ratchets up
                "strategy_name": getattr(signal, "strategy_name", self.strategy_name),
                "quantity": qty,
                "atr": signal.atr,
                "side": "long",
                "entry_time": datetime.now(timezone.utc).isoformat(),
            }
            self.db_manager.upsert_position(Position(
                symbol=signal.symbol,
                quantity=qty,
                average_price=order.price or signal.entry_price,
                entry_time=datetime.now(timezone.utc),
                side="long",
                unrealized_pnl=0.0,
            ))

            self.risk_manager.record_trade_opened()

            TradeLogger.log_entry(
                trade_id, signal.symbol, "BUY", qty,
                order.price or signal.entry_price, signal.stop_loss,
                signal.atr, signal.rsi, signal.choppiness, signal.volume_ratio,
                signal.orb_high, signal.orb_low, signal.trend_bias, settings.mode,
                signal.conditions,
            )

            msg = (
                f"{'📝' if settings.mode == 'paper' else '🟢'} ENTRY: {signal.symbol} "
                f"@ ₹{order.price:.2f} | SL: ₹{signal.stop_loss:.2f} | Qty: {qty} | "
                f"Confidence: {signal.confidence*100:.0f}%"
            )
            self.notify(msg)
            return trade_id

        except OrderError as e:
            TradeLogger.log_error("TradingEngine.execute_signal", e, {"symbol": signal.symbol})
            logger.error("Order failed for %s: %s", signal.symbol, e)
            return None

    # ─── Main run loop ────────────────────────────────────────────────────────

    def execute_multi_signal(self, signal: Any) -> Optional[str]:
        """Execute a `StrategySignal` from the new multi-strategy engine
        (EMA_TREND / ORB) through the same safety pipeline as the legacy
        `execute_signal` — risk checks, position sizing, order placement,
        position tracking. Kept as a separate method (rather than forcing
        StrategySignal into SignalResult's shape) because the two signal
        types carry genuinely different fields."""
        if signal.signal != "BUY":
            return None

        allowed, reason = self.risk_manager.can_take_trade(signal.symbol)
        if not allowed:
            TradeLogger.log_risk_event("BLOCKED", reason, signal.symbol)
            logger.info("Trade blocked for %s: %s", signal.symbol, reason)
            return None

        qty = self.position_sizer.calculate(
            entry_price=signal.entry_price,
            stop_loss_price=signal.stop_loss,
        )

        # Options trade in fixed lots, not arbitrary share counts. Round
        # down to the nearest whole lot (minimum 1 lot) — placing a
        # non-lot-multiple order would just get rejected by the exchange,
        # or worse, silently misprice the real risk being taken.
        selected_contract = signal.indicators.get("selected_contract") if signal.indicators else None
        if selected_contract:
            lot_size = LOT_SIZES.get(signal.symbol.upper(), 1)
            qty = max(lot_size, (qty // lot_size) * lot_size)

            # Exchange quantity-freeze limit — an order above this is
            # rejected outright, not just flagged. Cap rather than split.
            freeze_limit = FREEZE_QUANTITY_LIMITS.get(signal.symbol.upper())
            if freeze_limit and qty > freeze_limit:
                capped_qty = (freeze_limit // lot_size) * lot_size
                logger.warning(
                    "%s order size %d exceeds exchange freeze limit %d — capping to %d",
                    signal.symbol, qty, freeze_limit, capped_qty,
                )
                qty = max(lot_size, capped_qty)

        if qty <= 0:
            logger.warning("Quantity 0 for %s — skipping", signal.symbol)
            return None

        trade_id = str(uuid.uuid4())
        try:
            req = OrderRequest(
                symbol=signal.symbol, side="BUY", quantity=qty,
                price=signal.entry_price, order_type="MARKET",
            )
            order = self.order_manager.place_order(req)

            trade = Trade(
                id=trade_id, symbol=signal.symbol, side="long", quantity=qty,
                price=order.price or signal.entry_price,
                timestamp=datetime.now(timezone.utc), strategy=signal.strategy_name,
                status="filled", pnl=None,
                notes=f"confidence={signal.confidence:.1f} strategy={signal.strategy_name}",
            )
            self.db_manager.insert_trade(trade)

            self._open_positions[signal.symbol] = {
                "trade_id": trade_id,
                "entry_price": order.price or signal.entry_price,
                "stop_loss": signal.stop_loss,
                "target": signal.target,
                "trailing_stop": signal.stop_loss,
                "strategy_name": signal.strategy_name,
                "quantity": qty,
                "atr": signal.indicators.get("atr", 0.0),
                "side": "long",
                "entry_time": datetime.now(timezone.utc).isoformat(),
                # For OPTION_PREMIUM positions: the actual contract being
                # held. Exit monitoring MUST watch this contract's own
                # price, not the underlying index's — a NIFTY option's
                # stop-loss is in premium terms (e.g. ₹140), which has no
                # relationship to the index's spot price (e.g. 22,150).
                "contract_instrument_key": (
                    selected_contract.get("instrument_key") if selected_contract else None
                ),
                "expiry_date": signal.indicators.get("expiry_date") if signal.indicators else None,
            }
            self.db_manager.upsert_position(Position(
                symbol=signal.symbol, quantity=qty,
                average_price=order.price or signal.entry_price,
                entry_time=datetime.now(timezone.utc), side="long", unrealized_pnl=0.0,
            ))

            self.risk_manager.record_trade_opened()

            TradeLogger.log_entry(
                trade_id, signal.symbol, "BUY", qty,
                order.price or signal.entry_price, signal.stop_loss,
                signal.indicators.get("atr", 0.0), signal.indicators.get("rsi", 0.0),
                0.0, signal.indicators.get("volume_ratio", 0.0),
                signal.indicators.get("orb_high", 0.0), signal.indicators.get("orb_low", 0.0),
                "BULLISH", settings.mode, signal.conditions,
            )

            msg = (
                f"{'📝' if settings.mode == 'paper' else '🟢'} ENTRY: {signal.symbol} "
                f"@ ₹{order.price:.2f} | SL: ₹{signal.stop_loss:.2f} | Target: ₹{signal.target:.2f} | "
                f"Qty: {qty} | {signal.strategy_name} ({signal.confidence:.0f}% confidence)"
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

        This is the fix for a real bug: stop-loss/target were computed and
        stored on entry, but nothing ever checked them intraday — the only
        exit path was the 14:45 time-based force-exit. A position could run
        straight through its own stop-loss for hours. This now enforces the
        same exit logic `get_open_positions_detail()` merely *displayed*
        before, and also asks each position's strategy for a strategy-
        specific exit reason (e.g. EMA trend reversal, ORB range breakdown).
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

            if not current_price and contract_key:
                # Option contracts aren't subscribed on the live WebSocket
                # feed (they're expiry-specific and created dynamically), so
                # fall back to the latest historical candle's close as the
                # current-price proxy.
                try:
                    recent = self.client.get_historical_candles(contract_key, "5minute", limit=1)
                    current_price = recent[-1]["close"] if recent else None
                except Exception:
                    current_price = None
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
                exit_reason = "STOP_LOSS_HIT" if trail["stage"] == 0 else "TRAILING_STOP_HIT"
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
        """"At least one good trade per day" — but never a fabricated one.

        Fires at most once per day, only if:
          - the feature is enabled,
          - zero real trades have been taken today,
          - there was a real near-miss candidate seen during today's scans
            (tracked in `_best_of_day`, populated only from genuine
            `evaluate_all_strategies()` results, never invented),
          - it's at/after the configured trigger time.

        The candidate is re-evaluated FRESH right here (never replays the
        stale signal from earlier in the day) and only taken if it still
        clears the relaxed floor with a real, computed entry price.
        Returns the trade_id if a trade was taken, else None.
        """
        if not (
            self.enable_daily_floor_trade
            and not self._daily_floor_taken
            and self._trades_taken_today == 0
            and self._best_of_day is not None
            and now.hour == self.daily_floor_trigger_hour
            and now.minute >= self.daily_floor_trigger_minute
        ):
            return None

        self._daily_floor_taken = True  # only ever attempt once per day
        candidate_symbol = self._best_of_day["symbol"]
        universe_mode = self._best_of_day.get("mode", "STOCKS")

        if universe_mode == "OPTIONS":
            fresh_best = self.evaluate_option_premium(candidate_symbol)
        else:
            fresh_signals = self.evaluate_all_strategies(candidate_symbol)
            fresh_best = max(fresh_signals, key=lambda s: s.confidence, default=None)

        if fresh_best is None or fresh_best.confidence < self.daily_floor_confidence or fresh_best.entry_price <= 0:
            logger.info(
                "Daily floor trade skipped for %s — no longer clears even the "
                "relaxed floor on a fresh check", candidate_symbol,
            )
            return None

        fresh_best.signal = "BUY"
        logger.info(
            "Daily floor trade: %s via %s at %.0f%% confidence "
            "(below full threshold, above the %.0f%% floor — no signal was fabricated)",
            candidate_symbol, fresh_best.strategy_name, fresh_best.confidence, self.daily_floor_confidence,
        )
        trade_id = self.execute_multi_signal(fresh_best)
        if trade_id:
            self._trades_taken_today += 1
            self.notify(
                f"📌 Daily floor trade taken: {candidate_symbol} "
                f"({fresh_best.confidence:.0f}% confidence) — no other setup cleared full threshold today."
            )
        return trade_id

    async def run_trading_session(self) -> None:
        """Main async trading loop — runs during market hours.

        Fixes vs. the previous version:
          - Watchlist now comes from the real, saved UniverseConfig (item #4)
            instead of a hardcoded NIFTY50 slice.
          - Open positions are checked against stop-loss/target/trailing-stop
            and strategy-specific exit conditions on EVERY loop iteration —
            previously the only exit path was the 14:45 force-exit, so a
            losing position could run unmonitored for hours.
          - Entries now run the full multi-strategy engine (EMA_TREND + ORB),
            not just the legacy ORB-only path, and take whichever has higher
            confidence.
          - Optional "daily floor trade": if the whole entry window is about
            to close with zero trades taken, and there was a real (not
            fabricated) near-miss setup during the day, it's re-checked
            fresh and taken if it still clears a relaxed — but real —
            confidence floor. This is what "take at least one trade a day"
            means here: never inventing a signal, just not discarding the
            day's best genuine near-miss.
        """
        logger.info("Trading session starting")

        while BotState.is_running():
            try:
                now = datetime.now(IST)
                watchlist, universe_mode = self._resolve_universe()

                # Pre-market: capture ORB (9:15–9:30) for the legacy cache
                # (still used by evaluate_signal(), kept for diagnostics use).
                # Not applicable in OPTIONS mode — ORB is a stock/index price
                # breakout concept, not a premium-trading one.
                if universe_mode == "STOCKS" and now.hour == 9 and 15 <= now.minute < 30:
                    for sym in watchlist:
                        if sym not in self._orb_levels:
                            orb = self._capture_orb(sym)
                            if orb:
                                self._orb_levels[sym] = orb
                                logger.info("ORB captured for %s: H=%.2f L=%.2f", sym, orb["orb_high"], orb["orb_low"])

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
                    for sym in watchlist:
                        if not BotState.is_running():
                            break
                        if sym in self._open_positions:
                            continue

                        if universe_mode == "OPTIONS":
                            # Trading the index's OPTION PREMIUM (CE/PE),
                            # not the index itself — expiry and underlying
                            # trend are auto-detected for real, unattended.
                            best = self.evaluate_option_premium(sym)
                            signals = [best]
                        else:
                            signals = self.evaluate_all_strategies(sym)
                            best = MultiStrategyEngine.best_signal(signals)

                        if best is not None and best.signal == "BUY":
                            trade_id = self.execute_multi_signal(best)
                            if trade_id:
                                self._trades_taken_today += 1
                        else:
                            # Track the single best near-miss of the day for
                            # the optional floor-trade check below. Only a
                            # real, freshly-computed signal is ever tracked —
                            # nothing here is invented.
                            for sig in signals:
                                if sig.signal == "NONE" and sig.confidence >= self.daily_floor_confidence:
                                    if not self._best_of_day or sig.confidence > self._best_of_day["confidence"]:
                                        self._best_of_day = {"symbol": sym, "confidence": sig.confidence, "mode": universe_mode}

                    # Daily floor trade — see _maybe_take_daily_floor_trade().
                    self._maybe_take_daily_floor_trade(now)

                # Reset daily state at end of day
                if now.hour == 15 and now.minute >= 30:
                    self._orb_levels.clear()
                    self.risk_manager.reset_for_new_day()
                    self._trades_taken_today = 0
                    self._daily_floor_taken = False
                    self._best_of_day = None

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
        """Close an open position."""
        pos = self._open_positions.get(symbol)
        if not pos:
            return
        try:
            req = OrderRequest(symbol=symbol, side="SELL", quantity=pos["quantity"], order_type="MARKET")
            order = self.order_manager.place_order(req)
            exit_price = order.price or pos["entry_price"]
            gross_pnl = (exit_price - pos["entry_price"]) * pos["quantity"]
            brokerage  = (pos["entry_price"] + exit_price) * pos["quantity"] * 0.0003
            net_pnl    = gross_pnl - brokerage
            pnl_r      = gross_pnl / ((pos["entry_price"] - pos["stop_loss"]) * pos["quantity"]) if pos["stop_loss"] else 0

            self.risk_manager.record_trade_result(net_pnl)
            self.db_manager.delete_position(symbol)
            del self._open_positions[symbol]

            TradeLogger.log_exit(
                pos["trade_id"], symbol, exit_price, reason,
                gross_pnl, net_pnl, pnl_r, 0, 1, settings.mode,
            )
            pnl_icon = "✅" if net_pnl >= 0 else "❌"
            self.notify(f"{pnl_icon} EXIT: {symbol} @ ₹{exit_price:.2f} | PnL: ₹{net_pnl:.0f} ({reason})")

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
        """Everything item #7 asks for, per open position: symbol, entry
        price, target, stop-loss, live-updated trailing SL, current P&L,
        and which strategy opened it. Current price comes from the real
        Upstox v3 WebSocket cache when available; if there's no live tick
        yet, current_price is null and pnl is null — never fabricated."""
        try:
            from backend.api.websocket import get_prices_by_symbol
            live_prices = get_prices_by_symbol()
        except Exception:
            live_prices = {}

        details: List[Dict[str, Any]] = []
        for symbol, pos in self._open_positions.items():
            # Same fix as _monitor_open_positions: an option position must
            # be priced by its own contract, not the underlying index.
            contract_key = pos.get("contract_instrument_key")
            tick = live_prices.get(contract_key or symbol)
            current_price = tick.get("ltp") if tick else None
            if not current_price and contract_key:
                try:
                    recent = self.client.get_historical_candles(contract_key, "5minute", limit=1)
                    current_price = recent[-1]["close"] if recent else None
                except Exception:
                    current_price = None

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
            })
        return details

    def should_exit(self, position: dict, current_price: float) -> bool:
        return self.exit_manager.should_exit(position, current_price)
