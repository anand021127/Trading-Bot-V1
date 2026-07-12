"""Realistic backtest engine — item #6.

The old backtest router had a `_make_synthetic_candles()` fallback that
fabricated random price data whenever a real fetch failed or came back
short, and its ORB/EMA simulators assumed a fixed 16-bars/day intraday
structure that silently broke on daily candles. That's why a full year
produced "2 trades" — it was usually testing against structurally-wrong
data, not the real strategy.

This engine:
  - Takes already-fetched real candles (fetching is the router's job, so
    this stays pure/testable) and walks forward bar-by-bar.
  - Uses the SAME `MultiStrategyEngine` (EMA Trend / ORB) that live trading
    and the scanner use — the backtest tests what actually trades, not a
    parallel simulation that can silently diverge from it.
  - Records every signal, taken or rejected, with reasons — never hidden.
  - Applies realistic brokerage + slippage + STT on every trade.
  - Never fabricates candles. If a symbol has too little real data, it's
    reported as skipped with an explicit reason, not padded with noise.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.strategy.strategy_engine import MultiStrategyEngine
from backend.strategy.signal import StrategySignal


@dataclass
class CostConfig:
    commission_pct: float = 0.0003
    slippage_pct: float = 0.0001
    stt_pct: float = 0.001
    stamp_duty_pct: float = 0.00003

    def apply(self, entry: float, exit_price: float, qty: int) -> Dict[str, float]:
        buy_val = entry * qty
        sell_val = exit_price * qty
        gross_pnl = sell_val - buy_val
        brokerage = (buy_val + sell_val) * self.commission_pct
        slippage = (buy_val + sell_val) * self.slippage_pct
        stt = sell_val * self.stt_pct
        stamp_duty = buy_val * self.stamp_duty_pct
        charges = brokerage + slippage + stt + stamp_duty
        return {
            "gross_pnl": round(gross_pnl, 2),
            "net_pnl": round(gross_pnl - charges, 2),
            "brokerage": round(brokerage, 2),
            "slippage": round(slippage, 2),
            "stt": round(stt, 2),
            "charges": round(charges, 2),
        }


@dataclass
class RejectedSignal:
    symbol: str
    timestamp: str
    strategy: str
    reasons: List[str]


@dataclass
class BacktestTrade:
    symbol: str
    strategy: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    quantity: int
    exit_reason: str
    gross_pnl: float
    net_pnl: float
    charges: float
    confidence: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "strategy": self.strategy,
            "entry_time": self.entry_time, "exit_time": self.exit_time,
            "entry_price": self.entry_price, "exit_price": self.exit_price,
            "quantity": self.quantity, "exit_reason": self.exit_reason,
            "gross_pnl": self.gross_pnl, "net_pnl": self.net_pnl,
            "charges": self.charges, "confidence": self.confidence,
        }


@dataclass
class BacktestResult:
    total_candles_scanned: int = 0
    signals_generated: int = 0
    trades_taken: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    accuracy_pct: float = 0.0
    profit_factor: float = 0.0
    net_profit: float = 0.0
    net_profit_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    total_charges: float = 0.0
    equity_curve: List[Dict[str, Any]] = field(default_factory=list)
    trade_log: List[Dict[str, Any]] = field(default_factory=list)
    rejected_signals_sample: List[Dict[str, Any]] = field(default_factory=list)
    rejected_signals_total_count: int = 0
    rejection_reason_counts: Dict[str, int] = field(default_factory=dict)
    skipped_symbols: List[Dict[str, str]] = field(default_factory=list)
    data_source: str = "real_upstox_v3"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_candles_scanned": self.total_candles_scanned,
            "signals_generated": self.signals_generated,
            "trades_taken": self.trades_taken,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "accuracy_pct": round(self.accuracy_pct, 2),
            "profit_factor": round(self.profit_factor, 2),
            "net_profit": round(self.net_profit, 2),
            "net_profit_pct": round(self.net_profit_pct, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "total_charges": round(self.total_charges, 2),
            "equity_curve": self.equity_curve,
            "trade_log": self.trade_log,
            "rejected_signals_sample": self.rejected_signals_sample,
            "rejected_signals_total_count": self.rejected_signals_total_count,
            "rejection_reason_counts": self.rejection_reason_counts,
            "skipped_symbols": self.skipped_symbols,
            "data_source": self.data_source,
        }


class BacktestEngine:
    def __init__(
        self,
        strategy_engine: Optional[MultiStrategyEngine] = None,
        costs: Optional[CostConfig] = None,
        capital: float = 100000.0,
        risk_pct_per_trade: float = 0.01,
        min_candles_required: int = 60,
        rejected_sample_size: int = 200,
        max_window_bars: int = 400,
    ) -> None:
        self.strategy_engine = strategy_engine or MultiStrategyEngine()
        self.costs = costs or CostConfig()
        self.capital = capital
        self.risk_pct_per_trade = risk_pct_per_trade
        self.min_candles_required = min_candles_required
        self.rejected_sample_size = rejected_sample_size
        # Bounds how much history each strategy evaluation looks back over.
        # 400 5-minute bars ≈ 5-6 trading sessions — enough for EMA50/RSI14/
        # ATR14 warmup AND for ORB to always have the *current* day's first
        # bars in view. Without this bound, a full year of 5-minute candles
        # means every single bar re-scans the entire dataset-to-date, which
        # is both O(n²) slow and (before the day-aware ORB fix) part of why
        # ORB was anchored to day-1's range for the whole year.
        self.max_window_bars = max_window_bars

    def run(
        self,
        symbol_candles: Dict[str, List[Dict[str, Any]]],
        strategy_names: Optional[List[str]] = None,
    ) -> BacktestResult:
        result = BacktestResult()
        equity = self.capital
        peak = self.capital
        equity_curve: List[Dict[str, Any]] = [{"timestamp": "start", "equity": round(equity, 2)}]
        all_trades: List[BacktestTrade] = []
        rejected_sample: List[RejectedSignal] = []
        reason_counts: Dict[str, int] = {}
        rejected_total = 0
        signals_generated = 0
        candles_scanned = 0

        strategy_names = strategy_names or ["EMA_TREND", "ORB"]

        for symbol, candles in symbol_candles.items():
            if len(candles) < self.min_candles_required:
                result.skipped_symbols.append({
                    "symbol": symbol,
                    "reason": f"Only {len(candles)} real candles available, "
                              f"need at least {self.min_candles_required}. "
                              f"Not padded with synthetic data.",
                })
                continue

            candles_scanned += len(candles)
            position: Optional[Dict[str, Any]] = None

            for i in range(self.min_candles_required, len(candles)):
                window = candles[max(0, i + 1 - self.max_window_bars): i + 1]
                bar = candles[i]

                if position is not None:
                    exit_reason = self._check_exit(position, bar, window)
                    if exit_reason:
                        exit_price = self._exit_price_for(position, bar, exit_reason)
                        qty = position["quantity"]
                        costs = self.costs.apply(position["entry_price"], exit_price, qty)
                        trade = BacktestTrade(
                            symbol=symbol, strategy=position["strategy"],
                            entry_time=position["entry_time"], exit_time=bar.get("timestamp", ""),
                            entry_price=position["entry_price"], exit_price=round(exit_price, 2),
                            quantity=qty, exit_reason=exit_reason,
                            gross_pnl=costs["gross_pnl"], net_pnl=costs["net_pnl"],
                            charges=costs["charges"], confidence=position["confidence"],
                        )
                        all_trades.append(trade)
                        equity += costs["net_pnl"]
                        peak = max(peak, equity)
                        dd = (peak - equity) / peak * 100 if peak > 0 else 0
                        result.max_drawdown_pct = max(result.max_drawdown_pct, dd)
                        equity_curve.append({
                            "timestamp": bar.get("timestamp", ""), "equity": round(equity, 2),
                        })
                        position = None
                    continue  # already in position (or just exited) — skip new entries this bar

                signals = self.strategy_engine.evaluate(symbol, window, strategy_names=strategy_names)
                best = MultiStrategyEngine.best_signal(signals)

                for sig in signals:
                    if sig.rejected_reasons:
                        rejected_total += 1
                        for reason in sig.rejected_reasons:
                            reason_counts[reason] = reason_counts.get(reason, 0) + 1
                        if len(rejected_sample) < self.rejected_sample_size:
                            rejected_sample.append(RejectedSignal(
                                symbol=symbol, timestamp=bar.get("timestamp", ""),
                                strategy=sig.strategy_name, reasons=sig.rejected_reasons,
                            ))

                if best is not None:
                    signals_generated += 1
                    risk_per_share = best.entry_price - best.stop_loss
                    if risk_per_share > 0:
                        risk_amount = equity * self.risk_pct_per_trade
                        qty = max(1, int(risk_amount / risk_per_share))
                        position = {
                            "strategy": best.strategy_name,
                            "entry_time": bar.get("timestamp", ""),
                            "entry_price": best.entry_price,
                            "stop_loss": best.stop_loss,
                            "target": best.target,
                            "quantity": qty,
                            "confidence": best.confidence,
                        }

            # Close any position still open at the end of this symbol's data.
            if position is not None and candles:
                last_bar = candles[-1]
                exit_price = last_bar["close"]
                qty = position["quantity"]
                costs = self.costs.apply(position["entry_price"], exit_price, qty)
                trade = BacktestTrade(
                    symbol=symbol, strategy=position["strategy"],
                    entry_time=position["entry_time"], exit_time=last_bar.get("timestamp", ""),
                    entry_price=position["entry_price"], exit_price=round(exit_price, 2),
                    quantity=qty, exit_reason="BACKTEST_END",
                    gross_pnl=costs["gross_pnl"], net_pnl=costs["net_pnl"],
                    charges=costs["charges"], confidence=position["confidence"],
                )
                all_trades.append(trade)
                equity += costs["net_pnl"]
                peak = max(peak, equity)

        # ── aggregate metrics ──────────────────────────────────────────────
        result.total_candles_scanned = candles_scanned
        result.signals_generated = signals_generated
        result.trades_taken = len(all_trades)
        result.rejected_signals_sample = [
            {"symbol": r.symbol, "timestamp": r.timestamp, "strategy": r.strategy, "reasons": r.reasons}
            for r in rejected_sample
        ]
        result.rejected_signals_total_count = rejected_total
        result.rejection_reason_counts = dict(
            sorted(reason_counts.items(), key=lambda kv: -kv[1])
        )

        if all_trades:
            wins = [t for t in all_trades if t.net_pnl > 0]
            losses = [t for t in all_trades if t.net_pnl <= 0]
            gross_win = sum(t.net_pnl for t in wins)
            gross_loss = abs(sum(t.net_pnl for t in losses))

            result.winning_trades = len(wins)
            result.losing_trades = len(losses)
            result.accuracy_pct = len(wins) / len(all_trades) * 100
            result.profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (
                float("inf") if gross_win > 0 else 0.0
            )
            result.net_profit = sum(t.net_pnl for t in all_trades)
            result.net_profit_pct = result.net_profit / self.capital * 100
            result.total_charges = sum(t.charges for t in all_trades)
            result.trade_log = [t.to_dict() for t in all_trades]
            result.equity_curve = equity_curve

        # profit_factor of inf isn't JSON-safe — cap it for the response.
        if result.profit_factor == float("inf"):
            result.profit_factor = 999.99

        return result

    # ── exit logic ─────────────────────────────────────────────────────────

    def _check_exit(
        self, position: Dict[str, Any], bar: Dict[str, Any], window: List[Dict[str, Any]],
    ) -> Optional[str]:
        if bar["low"] <= position["stop_loss"]:
            return "STOP_LOSS_HIT"
        if position["target"] > 0 and bar["high"] >= position["target"]:
            return "TARGET_HIT"
        strat_exit = self.strategy_engine.check_exits(
            position["strategy"], position, window,
        )
        if strat_exit:
            return strat_exit
        return None

    @staticmethod
    def _exit_price_for(position: Dict[str, Any], bar: Dict[str, Any], reason: str) -> float:
        if reason == "STOP_LOSS_HIT":
            return min(position["stop_loss"], bar["high"])
        if reason == "TARGET_HIT":
            return max(position["target"], bar["low"])
        return bar["close"]
