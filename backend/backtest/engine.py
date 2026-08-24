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

import bisect
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from backend.strategy.exit_manager import TrailingStopManager
from backend.strategy.strategy_engine import MultiStrategyEngine
from backend.strategy.signal import StrategySignal, SignalType
from backend.backtest.historical_contract_resolver import (
    DataQualityReport,
    get_nearest_expiry_for_date,
    build_trading_symbol,
)
from backend.backtest.options_data_layer import HistoricalOptionsDataLoader, INDEX_STRIKE_INTERVALS, INDEX_LOT_SIZES


@dataclass
class CostConfig:
    """Configurable realistic transaction and statutory cost model.
    
    Default parameters reflect current Upstox / Indian exchange regulations:
    - Brokerage: ₹20 per executed order leg (min(₹20, 0.05% turnover) for options/equity)
    - STT: 0.15% on premium turnover (sell-side only for options), 0.025% sell-side intraday equity
    - Exchange transaction charges: 0.05% (NSE Options) / 0.00325% (NSE Equity)
    - GST: 18% on (Brokerage + Exchange Charges + SEBI Charges)
    - SEBI Turnover Charges: ₹10 per crore (0.0001% / 0.000001 of total turnover)
    - Stamp Duty: 0.003% (0.00003) on BUY side turnover
    - Slippage: 0.01% (0.0001) of turnover
    """
    flat_brokerage_per_order: float = 20.0
    brokerage_pct_cap: float = 0.0005
    equity_brokerage_pct: float = 0.0005
    option_stt_pct: float = 0.0015
    equity_intraday_stt_pct: float = 0.00025
    equity_delivery_stt_pct: float = 0.001
    stt_pct: Optional[float] = None
    brokerage_pct: Optional[float] = None
    commission_pct: Optional[float] = None
    option_exchange_turnover_pct: float = 0.0005
    equity_exchange_turnover_pct: float = 0.0000325
    gst_pct: float = 0.18
    sebi_turnover_pct: float = 0.000001
    stamp_duty_pct: float = 0.00003
    slippage_pct: float = 0.0001

    def __post_init__(self) -> None:
        if self.commission_pct is not None and self.brokerage_pct is None:
            self.brokerage_pct = self.commission_pct
        if self.stt_pct is not None:
            self.equity_intraday_stt_pct = self.stt_pct
        if self.brokerage_pct is not None:
            self.equity_brokerage_pct = self.brokerage_pct

    def apply(self, entry: float, exit_price: float, qty: int, is_option: bool = False) -> Dict[str, float]:
        buy_val = entry * qty
        sell_val = exit_price * qty
        turnover = buy_val + sell_val
        gross_pnl = sell_val - buy_val

        # Brokerage calculation
        if is_option:
            buy_brokerage = min(self.flat_brokerage_per_order, buy_val * self.brokerage_pct_cap)
            sell_brokerage = min(self.flat_brokerage_per_order, sell_val * self.brokerage_pct_cap)
        else:
            buy_brokerage = min(self.flat_brokerage_per_order, buy_val * self.equity_brokerage_pct)
            sell_brokerage = min(self.flat_brokerage_per_order, sell_val * self.equity_brokerage_pct)
        brokerage = round(buy_brokerage + sell_brokerage, 2)

        # STT (sell-side only for options and intraday equity)
        stt_rate = self.option_stt_pct if is_option else self.equity_intraday_stt_pct
        stt = round(sell_val * stt_rate, 2)

        # Exchange transaction charges
        ex_rate = self.option_exchange_turnover_pct if is_option else self.equity_exchange_turnover_pct
        exchange_charges = round(turnover * ex_rate, 2)

        # SEBI turnover charges
        sebi_charges = round(turnover * self.sebi_turnover_pct, 4)

        # GST on Brokerage + Exchange + SEBI
        gst = round((brokerage + exchange_charges + sebi_charges) * self.gst_pct, 2)

        # Stamp duty on BUY side turnover
        stamp_duty = round(buy_val * self.stamp_duty_pct, 2)

        # Slippage on total turnover
        slippage = round(turnover * self.slippage_pct, 2)

        total_cost = round(brokerage + stt + exchange_charges + gst + sebi_charges + stamp_duty + slippage, 2)
        net_pnl = round(gross_pnl - total_cost, 2)

        return {
            "gross_pnl": round(gross_pnl, 2),
            "brokerage": brokerage,
            "stt": stt,
            "exchange_charges": exchange_charges,
            "gst": gst,
            "sebi_charges": sebi_charges,
            "stamp_duty": stamp_duty,
            "slippage": slippage,
            "total_cost": total_cost,
            "charges": total_cost,  # backwards compatibility
            "net_pnl": net_pnl,
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
    brokerage: float = 0.0
    stt: float = 0.0
    exchange_charges: float = 0.0
    gst: float = 0.0
    sebi_charges: float = 0.0
    stamp_duty: float = 0.0
    slippage: float = 0.0
    total_cost: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "quantity": self.quantity,
            "exit_reason": self.exit_reason,
            "gross_pnl": self.gross_pnl,
            "brokerage": self.brokerage,
            "stt": self.stt,
            "exchange_charges": self.exchange_charges,
            "gst": self.gst,
            "sebi_charges": self.sebi_charges,
            "stamp_duty": self.stamp_duty,
            "slippage": self.slippage,
            "total_cost": self.total_cost,
            "charges": self.charges,
            "net_pnl": self.net_pnl,
            "confidence": self.confidence,
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
    execution_resolution_mode: str = "CONSERVATIVE_STOP_FIRST"
    # V21-FINAL: data quality report for backtest transparency
    data_quality: Optional[DataQualityReport] = None
    # Detailed diagnostic counters for auditability
    candles_loaded: int = 0
    warmup_bars: int = 0
    candles_evaluated: int = 0
    trend_bullish: int = 0
    trend_bearish: int = 0
    trend_neutral: int = 0
    directional_signals: int = 0
    ce_intents: int = 0
    pe_intents: int = 0
    contract_resolution_attempts: int = 0
    contracts_resolved: int = 0
    contract_resolution_failures: int = 0
    contract_resolution_failures_breakdown: Dict[str, int] = field(default_factory=dict)
    option_premium_lookup_attempts: int = 0
    option_premiums_found: int = 0
    option_premium_missing: int = 0
    risk_rejections: int = 0
    risk_rejections_breakdown: Dict[str, int] = field(default_factory=dict)
    orders_created: int = 0
    trades_opened: int = 0
    trades_closed: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d = {
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
            "total_rejected": self.rejected_signals_total_count,
            "trades_executed": self.trades_taken,
            "rejection_reason_counts": self.rejection_reason_counts,
            "skipped_symbols": self.skipped_symbols,
            "data_source": self.data_source,
            "execution_resolution_mode": self.execution_resolution_mode,
            "data_quality": self.data_quality.to_dict() if self.data_quality else None,
            # Diagnostic counters
            "candles_loaded": self.candles_loaded,
            "warmup_bars": self.warmup_bars,
            "candles_evaluated": self.candles_evaluated,
            "trend_bullish": self.trend_bullish,
            "trend_bearish": self.trend_bearish,
            "trend_neutral": self.trend_neutral,
            "directional_signals": self.directional_signals,
            "ce_intents": self.ce_intents,
            "pe_intents": self.pe_intents,
            "contract_resolution_attempts": self.contract_resolution_attempts,
            "contracts_resolved": self.contracts_resolved,
            "contract_resolution_failures": self.contract_resolution_failures,
            "contract_resolution_failures_breakdown": self.contract_resolution_failures_breakdown,
            "option_premium_lookup_attempts": self.option_premium_lookup_attempts,
            "option_premiums_found": self.option_premiums_found,
            "option_premium_missing": self.option_premium_missing,
            "risk_rejections": self.risk_rejections,
            "risk_rejections_breakdown": self.risk_rejections_breakdown,
            "orders_created": self.orders_created,
            "trades_opened": self.trades_opened,
            "trades_closed": self.trades_closed,
        }
        return d


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
        self.trailing_stop_manager = TrailingStopManager()
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
        progress_callback: Optional[Any] = None,
        option_contexts: Optional[Dict[str, Dict[str, Any]]] = None,
        options_data_loader: Optional[HistoricalOptionsDataLoader] = None,
        require_real_options: bool = False,
    ) -> BacktestResult:
        """`progress_callback(dict)` — if given, called periodically with
        {"phase": "processing", "symbol": ..., "symbol_index": ..., "total_symbols": ...,
         "bar_index": ..., "total_bars": ...} so a long-running backtest (e.g. a full
        year of 5-minute NIFTY data) can report real progress to a polling
        client instead of the caller just waiting on a single request."""
        result = BacktestResult()
        result.candles_loaded = sum(len(c) for c in symbol_candles.values())
        equity = self.capital
        peak = self.capital
        equity_curve: List[Dict[str, Any]] = [{"timestamp": "start", "equity": round(equity, 2)}]
        all_trades: List[BacktestTrade] = []
        rejected_sample: List[RejectedSignal] = []
        reason_counts: Dict[str, int] = {}
        rejected_total = 0
        signals_generated = 0
        candles_scanned = 0

        strategy_names = strategy_names or ["OPTION_PREMIUM"]
        option_contexts = option_contexts or {}
        total_symbols = len(symbol_candles)

        for symbol_index, (symbol, candles) in enumerate(symbol_candles.items()):
            if len(candles) < self.min_candles_required:
                result.skipped_symbols.append({
                    "symbol": symbol,
                    "reason": f"Only {len(candles)} real candles available, "
                              f"need at least {self.min_candles_required}. "
                              f"Not padded with synthetic data.",
                })
                continue

            result.warmup_bars += self.min_candles_required
            candles_scanned += len(candles)
            position: Optional[Dict[str, Any]] = None
            total_bars = len(candles)

            symbol_context = option_contexts.get(symbol, {})
            trend_series = symbol_context.get("underlying_trend_series")
            trend_series_keys = sorted(trend_series.keys()) if trend_series else []

            for i in range(self.min_candles_required, len(candles)):
                result.candles_evaluated += 1
                if progress_callback and (i % 200 == 0 or i == len(candles) - 1):
                    try:
                        progress_callback({
                            "phase": "processing",
                            "symbol": symbol,
                            "symbol_index": symbol_index + 1,
                            "total_symbols": total_symbols,
                            "bar_index": total_bars if i == len(candles) - 1 else i,
                            "total_bars": total_bars,
                            "trades_so_far": len(all_trades),
                        })
                    except Exception:
                        pass  # progress reporting must never break the backtest itself

                window = candles[max(0, i + 1 - self.max_window_bars): i + 1]
                bar = candles[i]
                bar_timestamp = bar.get("timestamp", "")
                bar_date = bar_timestamp[:10] if bar_timestamp else ""
                spot_close = float(bar["close"])

                bar_context = {
                    **symbol_context,
                    "symbol": symbol,
                    "current_bar_date": bar_date,
                    "evaluation_date": bar_date,
                    "spot_price": spot_close,
                }
                if trend_series:
                    trend = self._trend_at(
                        trend_series, trend_series_keys, bar_timestamp,
                    )
                else:
                    trend = "NEUTRAL"
                bar_context["underlying_trend"] = trend

                if trend == "BULLISH":
                    result.trend_bullish += 1
                elif trend == "BEARISH":
                    result.trend_bearish += 1
                else:
                    result.trend_neutral += 1

                if position is not None:
                    # In real options mode, check exit on option contract candles if available
                    opt_bar = bar
                    if position.get("is_real_option") and options_data_loader:
                        c_key = position.get("contract_key", "")
                        candle_rec = options_data_loader.get_candle_at(c_key, bar_timestamp)
                        if candle_rec:
                            opt_bar = {
                                "timestamp": candle_rec.timestamp,
                                "open": candle_rec.open,
                                "high": candle_rec.high,
                                "low": candle_rec.low,
                                "close": candle_rec.close,
                                "volume": candle_rec.volume,
                            }

                    exit_reason = self._check_exit(position, opt_bar, window, context=bar_context)
                    if exit_reason:
                        exit_price = self._exit_price_for(position, opt_bar, exit_reason)
                        qty = position["quantity"]
                        costs = self.costs.apply(
                            position["entry_price"], exit_price, qty,
                            is_option=position["strategy"] == "OPTION_PREMIUM" or position.get("is_real_option", False),
                        )
                        trade = BacktestTrade(
                            symbol=position.get("contract_symbol", symbol),
                            strategy=position["strategy"],
                            entry_time=position["entry_time"], exit_time=bar.get("timestamp", ""),
                            entry_price=position["entry_price"], exit_price=round(exit_price, 2),
                            quantity=qty, exit_reason=exit_reason,
                            gross_pnl=costs["gross_pnl"], net_pnl=costs["net_pnl"],
                            charges=costs["charges"], confidence=position["confidence"],
                            brokerage=costs["brokerage"],
                            stt=costs["stt"],
                            exchange_charges=costs["exchange_charges"],
                            gst=costs["gst"],
                            sebi_charges=costs["sebi_charges"],
                            stamp_duty=costs["stamp_duty"],
                            slippage=costs["slippage"],
                            total_cost=costs["total_cost"],
                        )
                        all_trades.append(trade)
                        result.trades_closed += 1
                        equity += costs["net_pnl"]
                        peak = max(peak, equity)
                        dd = (peak - equity) / peak * 100 if peak > 0 else 0
                        result.max_drawdown_pct = max(result.max_drawdown_pct, dd)
                        equity_curve.append({
                            "timestamp": bar.get("timestamp", ""), "equity": round(equity, 2),
                        })
                        position = None
                    continue  # already in position (or just exited) — skip new entries this bar

                signals = self.strategy_engine.evaluate(
                    symbol, window, context=bar_context, strategy_names=strategy_names,
                )
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
                    result.directional_signals += 1
                    opt_type_intent = best.indicators.get("directional_intent") or best.indicators.get("option_type")
                    if not opt_type_intent:
                        opt_type_intent = "PE" if best.signal in (SignalType.SELL, getattr(SignalType, "BUY_PUT", SignalType.SELL)) else "CE"
                    if opt_type_intent == "CE":
                        result.ce_intents += 1
                    else:
                        result.pe_intents += 1
                    
                    # ── Strict Real Options Execution Handling ──
                    if require_real_options:
                        # Signal must resolve to a real historical option contract candle
                        result.contract_resolution_attempts += 1
                        option_type = opt_type_intent
                        bar_dt = datetime.fromisoformat(bar_date).date() if bar_date else date.today()
                        
                        if options_data_loader is None or not options_data_loader.is_data_available():
                            # Strictly flag DATA_UNAVAILABLE — Never create trade, never substitute spot
                            result.contract_resolution_failures += 1
                            result.contract_resolution_failures_breakdown["missing_option_data"] = (
                                result.contract_resolution_failures_breakdown.get("missing_option_data", 0) + 1
                            )
                            unavail_reason = f"DATA_UNAVAILABLE — No verified historical option contract dataset loaded for {symbol}"
                            rejected_total += 1
                            reason_counts[unavail_reason] = reason_counts.get(unavail_reason, 0) + 1
                            if len(rejected_sample) < self.rejected_sample_size:
                                rejected_sample.append(RejectedSignal(
                                    symbol=symbol, timestamp=bar_timestamp,
                                    strategy=best.strategy_name, reasons=[unavail_reason],
                                ))
                            continue

                        contract_res = options_data_loader.resolve_contract(symbol, bar_dt, spot_close, option_type)
                        if contract_res is None:
                            result.contract_resolution_failures += 1
                            result.contract_resolution_failures_breakdown["no_matching_contract"] = (
                                result.contract_resolution_failures_breakdown.get("no_matching_contract", 0) + 1
                            )
                            unavail_reason = f"DATA_UNAVAILABLE — Cannot resolve historical contract for {symbol} on {bar_date}"
                            rejected_total += 1
                            reason_counts[unavail_reason] = reason_counts.get(unavail_reason, 0) + 1
                            if len(rejected_sample) < self.rejected_sample_size:
                                rejected_sample.append(RejectedSignal(
                                    symbol=symbol, timestamp=bar_timestamp,
                                    strategy=best.strategy_name, reasons=[unavail_reason],
                                ))
                            continue

                        result.contracts_resolved += 1
                        c_key, exp_str, strike_val, opt_type = contract_res
                        
                        result.option_premium_lookup_attempts += 1
                        opt_candle = options_data_loader.get_candle_at(c_key, bar_timestamp)
                        if opt_candle is None:
                            result.option_premium_missing += 1
                            unavail_reason = f"DATA_UNAVAILABLE — Missing historical option candle for {c_key} at {bar_timestamp}"
                            rejected_total += 1
                            reason_counts[unavail_reason] = reason_counts.get(unavail_reason, 0) + 1
                            if len(rejected_sample) < self.rejected_sample_size:
                                rejected_sample.append(RejectedSignal(
                                    symbol=symbol, timestamp=bar_timestamp,
                                    strategy=best.strategy_name, reasons=[unavail_reason],
                                ))
                            continue

                        result.option_premiums_found += 1
                        # Real Option entry execution using verified historical option premium
                        opt_entry_price = float(opt_candle.close)
                        if opt_entry_price <= 0.0:
                            result.option_premium_missing += 1
                            unavail_reason = f"DATA_UNAVAILABLE — Invalid non-positive option premium for {c_key} at {bar_timestamp}"
                            rejected_total += 1
                            reason_counts[unavail_reason] = reason_counts.get(unavail_reason, 0) + 1
                            continue

                        # 20% stop loss on option premium
                        opt_stop_loss = round(max(1.0, opt_entry_price * 0.8), 2)
                        opt_target = round(opt_entry_price + 2 * (opt_entry_price - opt_stop_loss), 2)
                        
                        lot_size = INDEX_LOT_SIZES.get(symbol.upper(), 25)
                        risk_per_unit = opt_entry_price - opt_stop_loss
                        risk_amount = equity * self.risk_pct_per_trade
                        num_lots = max(1, int(risk_amount / (risk_per_unit * lot_size))) if risk_per_unit > 0 else 1
                        opt_qty = num_lots * lot_size
                        
                        max_affordable_qty = int(equity / opt_entry_price) if opt_entry_price > 0 else 0
                        if max_affordable_qty < lot_size:
                            result.risk_rejections += 1
                            result.risk_rejections_breakdown["insufficient_capital_for_lot"] = (
                                result.risk_rejections_breakdown.get("insufficient_capital_for_lot", 0) + 1
                            )
                            rej_reason = f"RISK_REJECTED — Insufficient equity (₹{equity:.2f}) for 1 lot ({lot_size}) of {c_key} @ ₹{opt_entry_price:.2f}"
                            rejected_total += 1
                            reason_counts[rej_reason] = reason_counts.get(rej_reason, 0) + 1
                            continue
                        opt_qty = min(opt_qty, (max_affordable_qty // lot_size) * lot_size)
                        if opt_qty <= 0:
                            result.risk_rejections += 1
                            result.risk_rejections_breakdown["zero_quantity_sized"] = (
                                result.risk_rejections_breakdown.get("zero_quantity_sized", 0) + 1
                            )
                            rej_reason = "RISK_REJECTED — Sized quantity is 0 lots"
                            rejected_total += 1
                            reason_counts[rej_reason] = reason_counts.get(rej_reason, 0) + 1
                            continue

                        result.orders_created += 1
                        result.trades_opened += 1
                        position = {
                            "strategy": best.strategy_name,
                            "symbol": symbol,
                            "contract_symbol": c_key,
                            "contract_key": c_key,
                            "expiry": exp_str,
                            "strike": strike_val,
                            "option_type": opt_type,
                            "is_real_option": True,
                            "entry_time": bar_timestamp,
                            "entry_price": opt_entry_price,
                            "stop_loss": opt_stop_loss,
                            "trailing_stop": opt_stop_loss,
                            "target": opt_target,
                            "quantity": opt_qty,
                            "confidence": best.confidence,
                        }
                        continue

                    # Spot / Equity default execution (when require_real_options=False)
                    risk_per_share = best.entry_price - best.stop_loss
                    if risk_per_share > 0:
                        risk_amount = equity * self.risk_pct_per_trade
                        qty = max(1, int(risk_amount / risk_per_share))

                        max_affordable_qty = int(equity / best.entry_price) if best.entry_price > 0 else 0
                        if max_affordable_qty < 1:
                            result.risk_rejections += 1
                            result.risk_rejections_breakdown["insufficient_capital"] = (
                                result.risk_rejections_breakdown.get("insufficient_capital", 0) + 1
                            )
                            position = None
                            continue
                        qty = min(qty, max_affordable_qty)

                        result.orders_created += 1
                        result.trades_opened += 1
                        position = {
                            "strategy": best.strategy_name,
                            "entry_time": bar.get("timestamp", ""),
                            "entry_price": best.entry_price,
                            "stop_loss": best.stop_loss,
                            "trailing_stop": best.stop_loss,
                            "target": best.target,
                            "quantity": qty,
                            "confidence": best.confidence,
                        }

            # Close any position still open at the end of this symbol's data.
            if position is not None and candles:
                last_bar = candles[-1]
                last_ts = last_bar.get("timestamp", "")
                exit_price = last_bar["close"]
                
                if position.get("is_real_option") and options_data_loader:
                    c_key = position.get("contract_key", "")
                    candle_rec = options_data_loader.get_candle_at(c_key, last_ts)
                    if candle_rec:
                        exit_price = candle_rec.close
                    else:
                        # Fallback to entry price or last known option price if candle missing at backtest end
                        exit_price = position["entry_price"]

                qty = position["quantity"]
                costs = self.costs.apply(
                    position["entry_price"], exit_price, qty,
                    is_option=position["strategy"] == "OPTION_PREMIUM" or position.get("is_real_option", False),
                )
                trade = BacktestTrade(
                    symbol=position.get("contract_symbol", symbol),
                    strategy=position["strategy"],
                    entry_time=position["entry_time"], exit_time=last_ts,
                    entry_price=position["entry_price"], exit_price=round(exit_price, 2),
                    quantity=qty, exit_reason="BACKTEST_END",
                    gross_pnl=costs["gross_pnl"], net_pnl=costs["net_pnl"],
                    charges=costs["charges"], confidence=position["confidence"],
                    brokerage=costs["brokerage"],
                    stt=costs["stt"],
                    exchange_charges=costs["exchange_charges"],
                    gst=costs["gst"],
                    sebi_charges=costs["sebi_charges"],
                    stamp_duty=costs["stamp_duty"],
                    slippage=costs["slippage"],
                    total_cost=costs["total_cost"],
                )
                all_trades.append(trade)
                result.trades_closed += 1
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

        # Build comprehensive data quality report
        dq = DataQualityReport()
        dq.lookahead_protection = True
        dq.synthetic_data_used = False
        dq.total_bars = candles_scanned
        dq.rejection_reasons = result.rejection_reason_counts
        if require_real_options and options_data_loader:
            dq.historical_contract_data = options_data_loader.is_data_available()
            avail_cnt = options_data_loader.available_candles_count() if callable(getattr(options_data_loader, "available_candles_count", None)) else 0
            avail_int = avail_cnt if isinstance(avail_cnt, int) else candles_scanned
            dq.bars_with_real_data = min(candles_scanned, avail_int)
            dq.contracts_resolved = len(all_trades)
            dq.contracts_unavailable = sum(
                count for r_name, count in result.rejection_reason_counts.items() if "DATA_UNAVAILABLE" in r_name
            )
        else:
            dq.historical_contract_data = False
            dq.bars_with_real_data = candles_scanned
            dq.contracts_resolved = len(all_trades)
            dq.contracts_unavailable = 0
        result.data_quality = dq

        return result

    # ── exit logic ─────────────────────────────────────────────────────────

    @staticmethod
    def _trend_at(trend_series: Dict[str, str], sorted_keys: List[str], timestamp: str) -> str:
        """Most recent trend label at or before `timestamp` — never a
        label from AFTER this bar (that would be lookahead bias). If
        `timestamp` is before every entry in the series (e.g. the very
        first bars, before the underlying's own EMA50 has warmed up),
        returns NEUTRAL rather than guessing a direction. `sorted_keys`
        is precomputed once per symbol by the caller, not resorted on
        every bar — this runs on every single bar of a potentially
        year-long backtest.
        """
        if not trend_series or not timestamp or not sorted_keys:
            return "NEUTRAL"
        idx = bisect.bisect_right(sorted_keys, timestamp) - 1
        if idx < 0:
            return "NEUTRAL"
        return trend_series[sorted_keys[idx]]

    def _check_exit(
        self,
        position: Dict[str, Any],
        bar: Dict[str, Any],
        window: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Conservative deterministic exit check.
        
        Execution Resolution Mode: CONSERVATIVE_STOP_FIRST
        If both stop loss and target are breached in the same candle:
        the stop loss is evaluated against the pre-existing stop level at bar open.
        If bar['low'] <= pre-existing stop, STOP_LOSS_HIT / TRAILING_STOP_HIT triggers first.
        """
        current_stop_at_bar_start = position.get("trailing_stop", position["stop_loss"])
        target = position.get("target", 0.0)

        # ── Conservative Stop First Check ──
        if bar["low"] <= current_stop_at_bar_start:
            return (
                "TRAILING_STOP_HIT"
                if current_stop_at_bar_start > position["stop_loss"]
                else "STOP_LOSS_HIT"
            )

        # Target Check (if stop was not hit)
        if target > 0 and bar["high"] >= target:
            return "TARGET_HIT"

        # Ratchet trailing stop with bar high
        trail = self.trailing_stop_manager.compute(
            entry_price=position["entry_price"],
            initial_stop=position["stop_loss"],
            current_price=bar["high"],
            current_stop=current_stop_at_bar_start,
        )
        position["trailing_stop"] = trail["stop"]

        if bar["low"] <= position["trailing_stop"]:
            return (
                "TRAILING_STOP_HIT"
                if position["trailing_stop"] > position["stop_loss"]
                else "STOP_LOSS_HIT"
            )

        strat_exit = self.strategy_engine.check_exits(
            position["strategy"], position, window, context=context,
        )
        if strat_exit:
            return strat_exit
        return None

    @staticmethod
    def _exit_price_for(position: Dict[str, Any], bar: Dict[str, Any], reason: str) -> float:
        if reason in ("STOP_LOSS_HIT", "TRAILING_STOP_HIT"):
            return min(position.get("trailing_stop", position["stop_loss"]), bar["high"])
        if reason == "TARGET_HIT":
            return max(position["target"], bar["low"])
        return bar["close"]
