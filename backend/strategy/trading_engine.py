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
from backend.strategy.exit_manager import ExitManager

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
settings = load_settings()


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
    """Thread-safe bot running state."""
    _running = False
    _kill_switch = False
    _start_time: Optional[datetime] = None
    _stop_reason: str = ""

    @classmethod
    def start(cls) -> None:
        cls._running = True
        cls._kill_switch = False
        cls._start_time = datetime.now(timezone.utc)
        cls._stop_reason = ""

    @classmethod
    def stop(cls, reason: str = "Manual stop") -> None:
        cls._running = False
        cls._stop_reason = reason

    @classmethod
    def kill(cls, reason: str = "Emergency kill switch") -> None:
        cls._kill_switch = True
        cls._running = False
        cls._stop_reason = reason
        TradeLogger.log_critical("BotState", f"KILL SWITCH ACTIVATED: {reason}")

    @classmethod
    def reset_kill(cls) -> None:
        cls._kill_switch = False

    @classmethod
    def is_running(cls) -> bool:
        return cls._running and not cls._kill_switch

    @classmethod
    def status(cls) -> Dict[str, Any]:
        return {
            "running": cls._running,
            "kill_switch_active": cls._kill_switch,
            "start_time": cls._start_time.isoformat() if cls._start_time else None,
            "stop_reason": cls._stop_reason,
            "uptime_seconds": int((datetime.now(timezone.utc) - cls._start_time).total_seconds())
            if cls._start_time and cls._running else 0,
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
                "quantity": qty,
                "atr": signal.atr,
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

    async def run_trading_session(self) -> None:
        """Main async trading loop — runs during market hours."""
        logger.info("Trading session starting")
        watchlist = self.NIFTY50[:settings.universe.max_stocks_in_watchlist]

        while BotState.is_running():
            try:
                now = datetime.now(IST)

                # Pre-market: capture ORB (9:15–9:30)
                if now.hour == 9 and 15 <= now.minute < 30:
                    for sym in watchlist:
                        if sym not in self._orb_levels:
                            orb = self._capture_orb(sym)
                            if orb:
                                self._orb_levels[sym] = orb
                                logger.info("ORB captured for %s: H=%.2f L=%.2f", sym, orb["orb_high"], orb["orb_low"])

                # Force exit all at 14:45
                if self._is_exit_all_time() and self._open_positions:
                    logger.info("14:45 — forcing exit of all positions")
                    for sym in list(self._open_positions.keys()):
                        await self._close_position(sym, "TIME_FORCE_EXIT")

                # Main entry loop (9:30–12:30)
                if self._is_entry_window() and self._is_market_open():
                    for sym in watchlist:
                        if not BotState.is_running():
                            break
                        signal = self.evaluate_signal(sym)
                        if signal.signal == "long":
                            self.execute_signal(signal)

                # Reset ORB at end of day
                if now.hour == 15 and now.minute >= 30:
                    self._orb_levels.clear()
                    self.risk_manager.reset_for_new_day()

                await asyncio.sleep(300)  # 5-minute candle interval

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

    def should_exit(self, position: dict, current_price: float) -> bool:
        return self.exit_manager.should_exit(position, current_price)
