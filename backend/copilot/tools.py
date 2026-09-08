"""The Copilot's tool layer — one function per capability in the spec's
tool list. Every function is read-only, wraps an EXISTING service instead
of re-implementing it, and returns a structured result that always
includes `available: bool` and, when unavailable, a `reason` — so both
the LLM adapter and any direct API caller can tell "no trade opportunity"
apart from "I couldn't check."

These are NOT bound to any particular LLM's function-calling schema by
design — `TOOL_REGISTRY` at the bottom maps name -> callable so any
adapter (local LLM, rule-based fallback, or a future different LLM) can
look tools up the same way.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional


def _unavailable(reason: str) -> Dict[str, Any]:
    return {"available": False, "reason": reason}


class CopilotTools:
    """Constructed with references to the app's existing singleton
    services (the same objects FastAPI stores on `app.state` — see
    backend/api/main.py). Nothing here creates a new WebSocket, a new
    DB connection pool, or a new strategy engine; it calls into what's
    already running. Any missing/None service degrades that one tool to
    `available: False`, not a crash."""

    def __init__(
        self,
        engine: Any = None,           # backend.strategy.trading_engine.TradingEngine
        db_manager: Any = None,       # backend.database.db_manager.DatabaseManager
        health_monitor: Any = None,   # backend.health.health_monitor.HealthMonitor
        risk_manager: Any = None,     # falls back to engine.risk_manager if None
        scanner: Any = None,
        ws_client: Any = None,
    ) -> None:
        self.engine = engine
        self.db_manager = db_manager or getattr(engine, "db_manager", None)
        self.health_monitor = health_monitor
        self.risk_manager = risk_manager or getattr(engine, "risk_manager", None)
        self.scanner = scanner
        self.ws_client = ws_client

    # ── Market data ──────────────────────────────────────────────────
    def get_market_status(self) -> Dict[str, Any]:
        if self.ws_client is None and self.engine is None:
            return _unavailable("No live market-data connection is configured in this process.")
        now = datetime.now(timezone.utc)
        try:
            from backend.broker.websocket_client import is_nse_market_open
            open_now = is_nse_market_open()
        except Exception:
            open_now = None  # genuinely unknown — do not guess
        connected = bool(getattr(self.ws_client, "is_connected", False)) if self.ws_client else False
        feed_status = getattr(self.ws_client, "market_data_status", None) if self.ws_client else None
        return {
            "available": True,
            "market_open": open_now,
            "websocket_connected": connected,
            "feed_status": feed_status,
            "checked_at": now.isoformat(),
        }

    def get_live_candles(self, symbol: str, timeframe: str = "5minute", limit: int = 100) -> Dict[str, Any]:
        """Uses `client.get_current_candles()` (historical context + TODAY's
        intraday candles merged) — NOT get_historical_candles() alone,
        which only serves settled/completed days and would silently
        return yesterday's last candle as "current" during a live
        session (the exact bug this fixes — see
        backend/broker/upstox_client.py:get_current_candles docstring).

        Computes a strict `data_status`: "LIVE" only if the last candle
        is within COPILOT_MAX_CANDLE_AGE_SECONDS of now, else "STALE".
        Callers (decision_engine.py) must check this and refuse to trade
        on STALE data — this function itself does not raise or block,
        it only reports the truth."""
        client = getattr(self.engine, "client", None)
        if client is None:
            return _unavailable("No broker client attached to the trading engine — cannot fetch candles.")
        try:
            if hasattr(client, "get_current_candles"):
                candles = client.get_current_candles(symbol, timeframe, limit=limit)
            else:
                # Older/mocked clients without the merged method — fall
                # back to historical-only, but this WILL be stale during
                # a live session, so mark it explicitly rather than lie.
                candles = client.get_historical_candles(symbol, timeframe, limit=limit)
        except Exception as e:
            return _unavailable(f"get_current_candles({symbol!r}) failed: {e}")
        if not candles:
            return _unavailable(f"Broker returned no candles for {symbol} ({timeframe}).")

        last_ts = candles[-1].get("timestamp")
        age_seconds = None
        try:
            ts = datetime.fromisoformat(last_ts)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_seconds = (datetime.now(timezone.utc) - ts).total_seconds()
        except Exception:
            pass

        from backend.copilot.config import load_copilot_settings
        max_age = load_copilot_settings().max_candle_age_seconds
        if age_seconds is None:
            data_status = "UNKNOWN"
        elif age_seconds <= max_age:
            data_status = "LIVE"
        else:
            data_status = "STALE"

        return {
            "available": True,
            "symbol": symbol,
            "timeframe": timeframe,
            "candles": candles,
            "latest_candle": candles[-1],
            "candle_count": len(candles),
            "candle_timestamp": last_ts,
            "last_candle_timestamp": last_ts,  # kept for backward compatibility with earlier callers
            "data_age_seconds": age_seconds,
            "data_status": data_status,
            "max_age_seconds": max_age,
        }

    def get_live_prices(self, symbols: List[str]) -> Dict[str, Any]:
        if self.ws_client is None:
            return _unavailable("No live WebSocket client attached — cannot read live tick prices.")
        out = {}
        for sym in symbols:
            q = self.ws_client.get_price(sym) if hasattr(self.ws_client, "get_price") else None
            out[sym] = q if q else None
        return {"available": True, "prices": out}

    def get_indicators(self, symbol: str, candles: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not candles or len(candles) < 20:
            return _unavailable(f"Not enough candle history for {symbol} to compute indicators (need >=20, have {len(candles)}).")
        try:
            from backend.indicators.ema import ema
            from backend.indicators.rsi import rsi
            from backend.indicators.atr import atr
            from backend.indicators.vwap import vwap
            from backend.indicators.choppiness import choppiness_index
            highs = [float(c["high"]) for c in candles]
            lows = [float(c["low"]) for c in candles]
            closes = [float(c["close"]) for c in candles]
            volumes = [float(c.get("volume", 0) or 0) for c in candles]

            # VWAP resets every session — slice to just today's bars (same
            # calendar date as the last candle) before computing it, per
            # backend/indicators/vwap.py's own documented contract.
            last_date = str(candles[-1].get("timestamp", ""))[:10]
            session_start = 0
            for i in range(len(candles) - 1, -1, -1):
                if str(candles[i].get("timestamp", ""))[:10] != last_date:
                    session_start = i + 1
                    break
            vwap_h, vwap_l, vwap_c, vwap_v = highs[session_start:], lows[session_start:], closes[session_start:], volumes[session_start:]

            atr_vals = atr(highs, lows, closes, 14) if len(closes) >= 15 else []
            vwap_vals = vwap(vwap_h, vwap_l, vwap_c, vwap_v) if vwap_c else []
            chop_vals = choppiness_index(highs, lows, closes, 14) if len(closes) >= 15 else []

            return {
                "available": True,
                "symbol": symbol,
                "last_close": closes[-1],
                "ema20": ema(closes, 20)[-1] if len(closes) >= 20 else None,
                "ema50": ema(closes, 50)[-1] if len(closes) >= 50 else None,
                "rsi": rsi(closes, 14)[-1] if len(closes) >= 15 else None,
                "atr": atr_vals[-1] if atr_vals else None,
                "vwap": vwap_vals[-1] if vwap_vals else None,
                "choppiness_index": chop_vals[-1] if chop_vals else None,
                "as_of": candles[-1].get("timestamp"),
            }
        except Exception as e:
            return _unavailable(f"Indicator calculation failed: {e}")

    def get_support_resistance(self, candles: List[Dict[str, Any]], lookback: int = 60) -> Dict[str, Any]:
        """Simple swing high/low pivot levels over the trailing window —
        no external service exists for this in the current repo, so this
        is a small, clearly-labeled deterministic addition, not fabricated
        data: every level is a real high/low that occurred in `candles`."""
        if not candles:
            return _unavailable("No candle data supplied.")
        window = candles[-lookback:]
        highs = [float(c["high"]) for c in window]
        lows = [float(c["low"]) for c in window]
        return {
            "available": True,
            "lookback_bars": len(window),
            "resistance": max(highs),
            "support": min(lows),
            "resistance_at": window[highs.index(max(highs))].get("timestamp"),
            "support_at": window[lows.index(min(lows))].get("timestamp"),
        }

    def get_nearest_expiry(self, underlying_symbol: str) -> Dict[str, Any]:
        client = getattr(self.engine, "client", None)
        if client is None:
            return _unavailable("No broker client attached — cannot resolve expiry.")
        try:
            expiry = client.get_nearest_expiry(underlying_symbol)
        except Exception as e:
            return _unavailable(f"get_nearest_expiry({underlying_symbol!r}) failed: {e}")
        if not expiry:
            return _unavailable(f"No upcoming expiry found for {underlying_symbol}.")
        return {"available": True, "underlying": underlying_symbol, "expiry": expiry}

    def get_option_chain(self, underlying_symbol: str, expiry_date: Optional[str] = None) -> Dict[str, Any]:
        """Real option chain via the SAME client OptionPremiumStrategy uses
        (backend/broker/upstox_client.py:get_option_chain — GET /option/chain).
        Auto-resolves the nearest expiry if none is given, exactly like
        `TradingEngine.evaluate_option_premium` does."""
        client = getattr(self.engine, "client", None)
        if client is None:
            return _unavailable("No broker client attached — cannot fetch the option chain.")
        if not expiry_date:
            exp_result = self.get_nearest_expiry(underlying_symbol)
            if not exp_result.get("available"):
                return exp_result
            expiry_date = exp_result["expiry"]
        try:
            chain = client.get_option_chain(underlying_symbol, expiry_date)
        except Exception as e:
            return _unavailable(f"get_option_chain({underlying_symbol!r}, {expiry_date!r}) failed: {e}")
        if not chain:
            return _unavailable(f"Broker returned an empty option chain for {underlying_symbol} {expiry_date}.")

        underlying_price = None
        try:
            quotes = client.get_multiple_quotes([underlying_symbol])
            underlying_price = quotes.get(underlying_symbol, {}).get("ltp")
        except Exception:
            pass  # summary still works without it, just without ATM/max-pain framing

        result: Dict[str, Any] = {
            "available": True, "underlying": underlying_symbol, "expiry": expiry_date,
            "contract_count": len(chain), "contracts": chain,
        }
        try:
            from backend.market_data.option_chain import summarize_chain
            summary = summarize_chain(underlying_symbol, expiry_date, chain, underlying_price)
            result["summary"] = summary.to_dict()
        except Exception as e:
            result["summary"] = None
            result["summary_error"] = str(e)
        return result

    def get_option_quote(self, instrument_key: str) -> Dict[str, Any]:
        """Live tick quote if the WS client has one cached (the contract
        must already be subscribed — see
        TradingEngine._subscribe_option_contract); otherwise this does NOT
        fabricate a quote by guessing — it reports unavailable."""
        if self.ws_client is not None and hasattr(self.ws_client, "get_price"):
            q = self.ws_client.get_price(instrument_key)
            if q:
                age = self.ws_client.get_tick_age(instrument_key) if hasattr(self.ws_client, "get_tick_age") else None
                return {"available": True, "instrument_key": instrument_key, "source": "websocket_tick",
                        "tick_age_seconds": age, **q}
        return _unavailable(f"No live tick cached for {instrument_key} — it may not be subscribed on the WebSocket. "
                             f"Use get_option_chain() for the latest REST snapshot instead.")

    # ── Strategy / decisions ────────────────────────────────────────
    def get_strategy_signals(self, symbol: str, candles: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Underlying-level strategies only (EMA/ORB/etc — whatever's
        registered besides OPTION_PREMIUM). For the option-premium signal
        with real strike/premium, use get_trade_plan(), which calls
        `engine.evaluate_option_premium()` — the actual production path."""
        if self.engine is None or not candles:
            return _unavailable("No trading engine attached, or no candle data supplied.")
        try:
            all_signals = self.engine.strategy_engine.evaluate(symbol, candles)
            best = self.engine.strategy_engine.best_signal(all_signals)
            return {
                "available": True,
                "signal": best.to_dict() if best is not None else None,
                "all_signals": [s.to_dict() for s in all_signals],
                "note": None if best is not None else "Every strategy returned NONE for this symbol right now.",
            }
        except Exception as e:
            return _unavailable(f"Strategy evaluation failed: {e}")

    def get_trade_plan(self, symbol: str) -> Dict[str, Any]:
        """The REAL option trade plan. Delegates to decision_engine, which
        calls `engine.evaluate_option_premium(symbol)` — the exact same
        method the live scanner (backend/scanner/live_scanner.py) already
        calls for this symbol. Strike/expiry/premium/OI/Greeks all come
        from that one real call; nothing here re-implements chain fetching,
        ATM selection, or liquidity filtering."""
        from backend.copilot.decision_engine import build_trade_plan_for_symbol
        return build_trade_plan_for_symbol(self, symbol)

    # ── Positions / risk / P&L ──────────────────────────────────────
    def get_open_positions(self) -> Dict[str, Any]:
        if self.db_manager is None:
            return _unavailable("No database manager attached.")
        try:
            positions = self.db_manager.get_open_positions()
            return {"available": True, "positions": [p.__dict__ if hasattr(p, "__dict__") else dict(p) for p in positions]}
        except Exception as e:
            return _unavailable(f"Failed to read open positions: {e}")

    def get_account_risk(self) -> Dict[str, Any]:
        if self.risk_manager is None:
            return _unavailable("No risk manager attached.")
        try:
            return {"available": True, **self.risk_manager.get_status()}
        except Exception as e:
            return _unavailable(f"Failed to read risk status: {e}")

    def get_daily_pnl(self) -> Dict[str, Any]:
        if self.db_manager is None:
            return _unavailable("No database manager attached.")
        try:
            today = datetime.now(timezone.utc).date().isoformat()
            trades = self.db_manager.list_trades(date_from=today, date_to=today)
            realized = sum(float(getattr(t, "pnl", 0) or (t["pnl"] if "pnl" in t.keys() else 0) or 0) for t in trades)
            return {"available": True, "date": today, "trades_today": len(trades), "realized_pnl": realized}
        except Exception as e:
            return _unavailable(f"Failed to compute daily P&L: {e}")

    def get_recent_trades(self, limit: int = 10) -> Dict[str, Any]:
        if self.db_manager is None:
            return _unavailable("No database manager attached.")
        try:
            trades = self.db_manager.list_trades()[:limit]
            return {"available": True, "trades": [dict(t) if hasattr(t, "keys") else t.__dict__ for t in trades]}
        except Exception as e:
            return _unavailable(f"Failed to read recent trades: {e}")

    # ── Health / diagnostics ─────────────────────────────────────────
    def get_bot_health(self) -> Dict[str, Any]:
        if self.health_monitor is None:
            return _unavailable("No HealthMonitor attached to this process.")
        try:
            return {"available": True, **self.health_monitor.snapshot()}
        except Exception as e:
            return _unavailable(f"Failed to read health snapshot: {e}")

    def get_recent_errors(self, limit: int = 20) -> Dict[str, Any]:
        log_path = os.path.join("logs", "errors.log")
        if not os.path.exists(log_path):
            return _unavailable(f"No error log found at {log_path}.")
        try:
            with open(log_path) as f:
                lines = f.readlines()[-limit:]
            return {"available": True, "recent_error_lines": [l.rstrip("\n") for l in lines]}
        except Exception as e:
            return _unavailable(f"Failed to read error log: {e}")

    def run_full_diagnostics(self) -> Dict[str, Any]:
        from backend.copilot.diagnostics import run_full_diagnostics
        return run_full_diagnostics(self)

    def run_backtest(self, symbol: str, candles: List[Dict[str, Any]], **kwargs) -> Dict[str, Any]:
        if not candles:
            return _unavailable("No candle data supplied for backtest.")
        try:
            from backend.backtest.engine import BacktestEngine
            bt = BacktestEngine(**kwargs)
            result = bt.run({symbol: candles}) if hasattr(bt, "run") else _unavailable("BacktestEngine.run signature not compatible.")
            return {"available": True, "result": result.__dict__ if hasattr(result, "__dict__") else result}
        except Exception as e:
            return _unavailable(f"Backtest failed: {e}")


def build_tool_registry(tools: CopilotTools) -> Dict[str, Callable[..., Dict[str, Any]]]:
    """name -> bound method, for the conversational layer / any LLM
    function-calling adapter to look up by the exact names in the spec."""
    return {
        "get_market_status": tools.get_market_status,
        "get_live_prices": tools.get_live_prices,
        "get_indicators": tools.get_indicators,
        "get_option_chain": tools.get_option_chain,
        "get_option_quote": tools.get_option_quote,
        "get_support_resistance": tools.get_support_resistance,
        "get_strategy_signals": tools.get_strategy_signals,
        "get_trade_plan": tools.get_trade_plan,
        "get_open_positions": tools.get_open_positions,
        "get_account_risk": tools.get_account_risk,
        "get_daily_pnl": tools.get_daily_pnl,
        "get_recent_trades": tools.get_recent_trades,
        "get_bot_health": tools.get_bot_health,
        "get_recent_errors": tools.get_recent_errors,
        "run_full_diagnostics": tools.run_full_diagnostics,
        "run_backtest": tools.run_backtest,
    }
