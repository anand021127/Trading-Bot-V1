"""Backtest router — realistic historical strategy simulation."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from backend.config.settings import load_settings
from backend.indicators.atr import calculate_atr
from backend.indicators.choppiness import choppiness_index
from backend.indicators.ema import calculate_ema
from backend.indicators.rsi import calculate_rsi

router = APIRouter()
settings = load_settings()

COMMISSION_PCT = 0.0003   # 0.03% per side (Zerodha/Upstox flat)
SLIPPAGE_PCT   = 0.0001   # 0.01% slippage estimate
STT_PCT        = 0.001    # STT on sell side only
STAMP_DUTY_PCT = 0.00003  # Stamp duty on buy side


class BacktestRequest(BaseModel):
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    commission_pct: Optional[float] = None
    slippage_pct: Optional[float] = None
    symbols: Optional[List[str]] = None
    capital: Optional[float] = None


def _apply_costs(entry: float, exit_p: float, qty: int) -> Dict[str, float]:
    """Calculate realistic transaction costs for a round trip."""
    comm_pct  = COMMISSION_PCT
    slip_pct  = SLIPPAGE_PCT
    buy_val   = entry * qty
    sell_val  = exit_p * qty
    gross_pnl = sell_val - buy_val
    brokerage = (buy_val + sell_val) * comm_pct
    slippage  = (buy_val + sell_val) * slip_pct
    stt       = sell_val * STT_PCT
    stamp     = buy_val  * STAMP_DUTY_PCT
    charges   = brokerage + slippage + stt + stamp
    net_pnl   = gross_pnl - charges
    return {
        "gross_pnl": round(gross_pnl, 2),
        "net_pnl": round(net_pnl, 2),
        "brokerage": round(brokerage, 2),
        "slippage": round(slippage, 2),
        "stt": round(stt, 2),
        "charges": round(charges, 2),
    }


def _simulate_trades(
    symbol: str,
    candles: List[Dict[str, Any]],
    capital: float,
    risk_pct: float = 0.01,
) -> List[Dict[str, Any]]:
    """Run ORB strategy simulation on historical candles."""
    if len(candles) < 50:
        return []

    closes  = [c["close"]  for c in candles]
    highs   = [c["high"]   for c in candles]
    lows    = [c["low"]    for c in candles]
    volumes = [c["volume"] for c in candles]

    ema20_all = calculate_ema(closes, 20)
    ema50_all = calculate_ema(closes, 50)
    rsi_all   = calculate_rsi(closes, 14)
    atr_all   = calculate_atr(highs, lows, closes, 14)
    ci_all    = choppiness_index(highs, lows, closes, 14)

    trades: List[Dict[str, Any]] = []
    in_trade = False
    entry_price = 0.0
    stop_loss   = 0.0
    trail_stop  = 0.0
    entry_idx   = 0
    qty         = 0
    highest_close = 0.0
    stage       = 1
    r_value     = 0.0

    # Rolling ORB (approximate using first 3 bars of each "session")
    session_high = highs[0]
    session_low  = lows[0]
    bar_count    = 0

    for i in range(20, len(candles)):
        bar_count += 1
        c = candles[i]
        close   = closes[i]
        high    = highs[i]
        low     = lows[i]
        vol     = volumes[i]

        # Reset session ORB every 30 bars (approx. 2.5h session)
        if bar_count % 78 < 3:
            session_high = high
            session_low  = low
        else:
            session_high = max(session_high, high)
            session_low  = min(session_low, low)

        orb_h = session_high
        orb_l = session_low

        if len(atr_all) <= i or len(rsi_all) <= i - 14:
            continue

        atr_i = atr_all[min(i, len(atr_all) - 1)]
        rsi_i = rsi_all[min(i - 14, len(rsi_all) - 1)] if rsi_all else 50
        ci_i  = ci_all[min(i - 14,  len(ci_all) - 1)]  if ci_all  else 50
        em20  = ema20_all[min(i, len(ema20_all) - 1)]
        em50  = ema50_all[min(i, len(ema50_all) - 1)]
        vol_avg = sum(volumes[max(0, i-20):i]) / 20 if i >= 20 else 1
        vol_ratio = vol / vol_avg if vol_avg > 0 else 0

        # Exit management
        if in_trade:
            highest_close = max(highest_close, close)

            # 4-stage trailing stop
            if close >= entry_price + 3 * r_value and stage < 4:
                stage = 4
                trail_stop = max(trail_stop, highest_close - atr_i)
            elif close >= entry_price + 2 * r_value and stage < 3:
                stage = 3
                trail_stop = max(trail_stop, highest_close - 1.5 * atr_i)
            elif close >= entry_price + r_value and stage < 2:
                stage = 2
                trail_stop = entry_price  # break even

            # Update trail in stage 3/4
            if stage >= 3:
                new_trail = highest_close - (atr_i if stage == 4 else 1.5 * atr_i)
                trail_stop = max(trail_stop, new_trail)

            exit_reason = None
            if low <= trail_stop:
                exit_reason = "TRAILING_STOP_HIT"
            elif em20 < em50 and stage >= 2:
                exit_reason = "EMA20_CROSS_BELOW"
            elif bar_count % 78 == 77:
                exit_reason = "TIME_FORCE_EXIT"

            if exit_reason:
                exit_p = max(trail_stop, low)
                costs  = _apply_costs(entry_price, exit_p, qty)
                dur    = i - entry_idx
                pnl_r  = costs["gross_pnl"] / (r_value * qty) if r_value and qty else 0
                trades.append({
                    "symbol": symbol,
                    "entry_idx": entry_idx,
                    "exit_idx": i,
                    "entry_price": round(entry_price, 2),
                    "exit_price": round(exit_p, 2),
                    "quantity": qty,
                    "exit_reason": exit_reason,
                    "stage_at_exit": stage,
                    "duration_bars": dur,
                    "pnl_r": round(pnl_r, 3),
                    **costs,
                })
                in_trade = False
                stage    = 1

        # Entry conditions
        elif not in_trade:
            body = abs(close - c["open"])
            rng  = high - low
            body_pct = body / rng if rng > 0 else 0
            trend_ok = em20 > em50 and close > em20
            orb_ok   = close > orb_h
            rsi_ok   = 55 <= rsi_i <= 75
            vol_ok   = vol_ratio >= 1.5
            ci_ok    = ci_i < 61.8
            body_ok  = body_pct >= 0.6
            atr_ok   = atr_i > 0

            passed = sum([trend_ok, orb_ok, rsi_ok, vol_ok, ci_ok, body_ok, atr_ok])
            if passed >= 6:
                sl       = close - 1.5 * atr_i
                r_value  = close - sl
                risk_amt = capital * risk_pct
                qty      = max(1, int(risk_amt / r_value)) if r_value > 0 else 1
                entry_price   = close * (1 + SLIPPAGE_PCT)
                stop_loss     = sl
                trail_stop    = sl
                highest_close = close
                entry_idx     = i
                stage         = 1
                in_trade      = True

    # Close any open position at end
    if in_trade and len(candles) > entry_idx:
        exit_p = closes[-1]
        costs  = _apply_costs(entry_price, exit_p, qty)
        pnl_r  = costs["gross_pnl"] / (r_value * qty) if r_value and qty else 0
        trades.append({
            "symbol": symbol,
            "entry_idx": entry_idx,
            "exit_idx": len(candles) - 1,
            "entry_price": round(entry_price, 2),
            "exit_price": round(exit_p, 2),
            "quantity": qty,
            "exit_reason": "SESSION_END",
            "stage_at_exit": stage,
            "duration_bars": len(candles) - 1 - entry_idx,
            "pnl_r": round(pnl_r, 3),
            **costs,
        })

    return trades


def _compute_metrics(trades: List[Dict], capital: float) -> Dict[str, Any]:
    """Compute professional performance metrics from a list of trades."""
    if not trades:
        return {}

    net_pnls = [t["net_pnl"] for t in trades]
    wins     = [p for p in net_pnls if p > 0]
    losses   = [p for p in net_pnls if p < 0]
    total    = len(net_pnls)
    win_rate = len(wins) / total * 100 if total else 0

    gross_wins   = sum(wins)
    gross_losses = abs(sum(losses))
    profit_factor = gross_wins / gross_losses if gross_losses else 0

    # Max drawdown from equity curve
    equity = capital
    peak   = capital
    max_dd = 0.0
    equity_curve = [{"value": capital}]
    for pnl in net_pnls:
        equity += pnl
        peak    = max(peak, equity)
        dd      = (peak - equity) / peak * 100 if peak > 0 else 0
        max_dd  = max(max_dd, dd)
        equity_curve.append({"value": round(equity, 2)})

    # Sharpe ratio (simplified)
    if len(net_pnls) > 1:
        mean_r  = sum(net_pnls) / len(net_pnls)
        var     = sum((r - mean_r) ** 2 for r in net_pnls) / len(net_pnls)
        std_r   = math.sqrt(var) if var > 0 else 1
        sharpe  = (mean_r / std_r) * math.sqrt(252) if std_r else 0
    else:
        sharpe = 0.0

    net_profit = sum(net_pnls)
    avg_win    = gross_wins / len(wins)   if wins   else 0
    avg_loss   = gross_losses / len(losses) if losses else 0
    expectancy = (win_rate/100 * avg_win) - ((1 - win_rate/100) * avg_loss)

    return {
        "total_trades":   total,
        "win_rate":       round(win_rate, 2),
        "profit_factor":  round(profit_factor, 2),
        "net_profit":     round(net_profit, 2),
        "net_profit_pct": round(net_profit / capital * 100, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_ratio":   round(sharpe, 2),
        "avg_win_r":      round(sum(t["pnl_r"] for t in trades if t["net_pnl"] > 0) / len(wins), 2) if wins else 0,
        "avg_loss_r":     round(sum(t["pnl_r"] for t in trades if t["net_pnl"] <= 0) / len(losses), 2) if losses else 0,
        "expectancy":     round(expectancy, 2),
        "gross_wins":     round(gross_wins, 2),
        "gross_losses":   round(gross_losses, 2),
        "total_charges":  round(sum(t["charges"] for t in trades), 2),
    }, equity_curve


@router.post("/run")
async def run_backtest(request: BacktestRequest) -> Dict[str, Any]:
    """Run ORB strategy backtest on synthetic price data (real data requires token)."""
    capital  = request.capital or settings.capital.total
    symbols  = request.symbols or ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK"]

    # Try real Upstox data, fall back to synthetic
    all_trades: List[Dict] = []
    data_source = "synthetic"

    try:
        from backend.broker.upstox_client import UpstoxClient
        client = UpstoxClient()
        if client.is_token_valid():
            for sym in symbols[:5]:  # limit to 5 to avoid rate limits
                try:
                    candles = client.get_historical_candles(
                        sym, "5minute",
                        from_date=request.start_date or "2024-01-01",
                        to_date=request.end_date or "2024-12-31",
                        limit=2000,
                    )
                    if candles and len(candles) >= 50:
                        sym_trades = _simulate_trades(sym, candles, capital / len(symbols))
                        all_trades.extend(sym_trades)
                        data_source = "live_upstox"
                except Exception:
                    pass
    except Exception:
        pass

    # Synthetic fallback
    if not all_trades:
        import random
        random.seed(42)
        for sym in symbols[:3]:
            prices = [2400.0]
            for _ in range(499):
                prices.append(max(100, prices[-1] + random.gauss(0.5, 15)))
            syn_candles = []
            for j, p in enumerate(prices):
                h = p + random.uniform(5, 20)
                l = p - random.uniform(5, 20)
                syn_candles.append({
                    "close": round(p, 2),
                    "high": round(h, 2),
                    "low": round(l, 2),
                    "open": round(p + random.uniform(-5, 5), 2),
                    "volume": random.randint(500000, 3000000),
                    "timestamp": f"2024-{(j//78+1):02d}-{(j%20+1):02d}T09:30:00",
                })
            sym_trades = _simulate_trades(sym, syn_candles, capital / len(symbols))
            all_trades.extend(sym_trades)

    if not all_trades:
        return {
            "total_trades": 0, "win_rate": 0, "profit_factor": 0,
            "net_profit": 0, "max_drawdown_pct": 0, "sharpe_ratio": 0,
            "avg_win_r": 0, "avg_loss_r": 0, "equity_curve": [],
            "monthly_returns": {}, "trade_log": [],
            "message": "No trades generated. Try different date range or symbols.",
            "data_source": data_source,
        }

    metrics_result = _compute_metrics(all_trades, capital)
    if isinstance(metrics_result, tuple):
        metrics, equity_curve = metrics_result
    else:
        metrics, equity_curve = metrics_result, []

    # Monthly returns from trades
    monthly: Dict[str, float] = {}
    for t in all_trades:
        month = f"2024-{(t.get('exit_idx', 0) // 78 + 1):02d}"
        monthly[month] = monthly.get(month, 0) + t["net_pnl"]

    # Format trade log for frontend
    trade_log = [
        {
            "symbol": t["symbol"],
            "entry_price": t["entry_price"],
            "exit_price": t["exit_price"],
            "quantity": t["quantity"],
            "exit_reason": t["exit_reason"],
            "gross_pnl": t["gross_pnl"],
            "net_pnl": t["net_pnl"],
            "pnl_r": t["pnl_r"],
            "charges": t["charges"],
            "stage_at_exit": t.get("stage_at_exit", 1),
            "duration_bars": t.get("duration_bars", 0),
        }
        for t in all_trades[:100]  # cap to 100 for response size
    ]

    return {
        **metrics,
        "equity_curve": equity_curve,
        "monthly_returns": {k: round(v, 2) for k, v in sorted(monthly.items())},
        "trade_log": trade_log,
        "data_source": data_source,
        "message": f"Backtest complete. Data source: {data_source}. "
                   f"{'Used real Upstox data.' if data_source == 'live_upstox' else 'Used synthetic data — connect token for real results.'}",
    }


@router.get("/status/{task_id}")
async def get_backtest_status(task_id: str) -> Dict[str, Any]:
    return {"task_id": task_id, "status": "completed", "progress_pct": 100}


@router.get("/result/{task_id}")
async def get_backtest_result(task_id: str) -> Dict[str, Any]:
    return {"task_id": task_id, "status": "completed", "trade_log": []}
