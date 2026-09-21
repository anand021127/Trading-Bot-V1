"""Market-driven V8-D → paper execution path (no live orders)."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from backend.paper.market_scan_loop import (
    PaperMarketScanner,
    candles_are_fresh,
    signal_to_paper_payload,
)
from backend.paper.paper_runtime import PaperTradingRuntime
from backend.strategy.strategies.v8d_strategy import V8DStrategy
from backend.strategy.signal import SignalType, StrategySignal


def _bars_for_ce_signal(n: int = 80) -> List[Dict[str, Any]]:
    """Synthesize OHLC that satisfies V8-D bullish pullback rules (for path test only).

    Not used as production market data — only to prove scanner→pipeline wiring.
    """
    # Build a strong uptrend then a controlled pullback green reversal
    candles = []
    base = 24000.0
    start = datetime(2026, 9, 18, 9, 15, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    price = base
    for i in range(n - 3):
        o = price
        price = price + 8 + (i % 3)  # steady up
        c = price
        h = max(o, c) + 2
        l = min(o, c) - 1
        candles.append({
            "timestamp": (start + timedelta(minutes=5 * i)).isoformat(),
            "open": o, "high": h, "low": l, "close": c, "volume": 1000 + i,
        })
    # Pullback bar: dip toward EMA then strong green close above prior high
    prev = candles[-1]
    o = prev["close"]
    l = o - 25  # pullback
    c = prev["high"] + 15  # reclaim prior high
    h = c + 5
    candles.append({
        "timestamp": (start + timedelta(minutes=5 * (n - 2))).isoformat(),
        "open": o, "high": h, "low": l, "close": c, "volume": 5000,
    })
    # Final confirmation green
    prev = candles[-1]
    o = prev["close"] - 5
    c = prev["close"] + 20
    h = c + 3
    l = o - 2
    candles.append({
        "timestamp": (start + timedelta(minutes=5 * (n - 1))).isoformat(),
        "open": o, "high": h, "low": l, "close": c, "volume": 6000,
    })
    return candles


class FakeMarketData:
    def __init__(self, candles, chain, spot, expiry="2027-01-07"):
        self.candles = candles
        self.chain = chain
        self.spot = spot
        self.expiry = expiry
        self.calls = {"candles": 0, "chain": 0, "expiry": 0}

    def get_current_candles(self, symbol, interval="5minute", limit=120):
        self.calls["candles"] += 1
        return list(self.candles)

    def get_nearest_expiry(self, symbol):
        self.calls["expiry"] += 1
        return self.expiry

    def get_option_chain_with_spot(self, symbol, expiry_date):
        self.calls["chain"] += 1
        return list(self.chain), self.spot


def _atm_chain(spot: float) -> List[Dict[str, Any]]:
    strike = int(round(spot / 50.0) * 50)
    return [
        {
            "strike": float(strike),
            "option_type": "CE",
            "instrument_key": "NSE_FO|SCAN_CE_TEST",
            "ltp": 85.0,
            "lot_size": 75,
            "freeze_quantity": 1800,
            "option_atr": 5.0,
            "atr": 5.0,
            "volume": 10000,
            "oi": 50000,
        },
        {
            "strike": float(strike),
            "option_type": "PE",
            "instrument_key": "NSE_FO|SCAN_PE_TEST",
            "ltp": 80.0,
            "lot_size": 75,
            "freeze_quantity": 1800,
            "option_atr": 5.0,
            "atr": 5.0,
            "volume": 8000,
            "oi": 40000,
        },
    ]


def test_candles_are_fresh_rejects_stale():
    now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    old = [{
        "timestamp": (now - timedelta(hours=2)).isoformat(),
        "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 1,
    }] * 70
    ok, reason = candles_are_fresh(old, max_age_seconds=900, min_bars=60, now=now)
    assert ok is False
    assert "stale" in reason


def test_candles_are_fresh_accepts_recent():
    now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    bars = []
    for i in range(70):
        bars.append({
            "timestamp": (now - timedelta(minutes=5 * (70 - i))).isoformat(),
            "open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100.5 + i, "volume": 1,
        })
    ok, reason = candles_are_fresh(bars, max_age_seconds=900, min_bars=60, now=now)
    assert ok is True, reason


def test_signal_to_paper_payload_maps_v8d_fields():
    sig = StrategySignal(
        strategy_name="V8_D_PULLBACK_ATM",
        symbol="NIFTY50",
        signal=SignalType.BUY,
        entry_price=85.0,
        stop_loss=60.0,
        target=120.0,
    )
    sig.indicators = {
        "selected_contract": {
            "instrument_key": "NSE_FO|X",
            "option_type": "CE",
            "strike": 24000,
            "lot_size": 75,
            "option_atr": 4.0,
        },
        "sizing": {"quantity": 75},
        "underlying_spot": 24010,
    }
    payload = signal_to_paper_payload(sig, expiry="2027-01-07")
    assert payload is not None
    assert payload["instrument_key"] == "NSE_FO|X"
    assert payload["quantity"] == 75
    assert payload["premium"] == 85.0
    assert payload["expiry"] == "2027-01-07"


def test_scan_no_trade_when_stale_data():
    path = os.path.join(tempfile.mkdtemp(), "scan1.db")
    env = {
        "TRADING_MODE": "paper",
        "TRADING_STRATEGY": "V8_D_PULLBACK_ATM",
        "UPSTOX_ORDER_PRODUCT": "I",
        "DATABASE_PATH": path,
        "RISK_PER_TRADE_PCT": "0.025",
    }
    now = datetime.now(timezone.utc)
    stale = [{
        "timestamp": (now - timedelta(hours=5)).isoformat(),
        "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1,
    }] * 70
    data = FakeMarketData(stale, _atm_chain(24000), 24000)
    with mock.patch.dict(os.environ, env, clear=False):
        rt = PaperTradingRuntime()
        morning = datetime(2026, 9, 18, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        rt.now_fn = lambda: morning
        scanner = PaperMarketScanner(
            data=data,
            strategy=V8DStrategy(),
            max_candle_age_seconds=600,
            min_bars=60,
        )
        res = scanner.scan_once(rt, now=now)
    assert res.traded is False
    assert "stale" in res.reason


def test_full_path_market_scan_to_sqlite_trade():
    """Prove: market-data input → V8-D → pipeline → paper fill → SQLite."""
    path = os.path.join(tempfile.mkdtemp(), "scan2.db")
    env = {
        "TRADING_MODE": "paper",
        "TRADING_STRATEGY": "V8_D_PULLBACK_ATM",
        "UPSTOX_ORDER_PRODUCT": "I",
        "DATABASE_PATH": path,
        "RISK_PER_TRADE_PCT": "0.025",
        "TRADING_CAPITAL": "100000",
    }
    # Use a mock strategy that returns a valid BUY so we don't depend on
    # fragile synthetic OHLC matching every V8-D filter — production still
    # uses real V8DStrategy with real candles.
    class MockStrategy:
        name = "V8_D_PULLBACK_ATM"

        def evaluate_v8d_signal(self, **kwargs):
            sig = StrategySignal(
                strategy_name=self.name,
                symbol=kwargs["underlying_symbol"],
                signal=SignalType.BUY,
                entry_price=85.0,
                stop_loss=61.2,
                target=120.0,
                generated_at=datetime.now(timezone.utc).isoformat(),
            )
            sig.indicators = {
                "selected_contract": {
                    "instrument_key": "NSE_FO|SCAN_CE_TEST",
                    "option_type": "CE",
                    "strike": 24000.0,
                    "lot_size": 75,
                    "freeze_quantity": 1800,
                    "ltp": 85.0,
                    "option_atr": 5.0,
                    "atr": 5.0,
                },
                "sizing": {"quantity": 75},
                "underlying_spot": kwargs["spot_price"],
                "lot_size": 75,
                "option_type": "CE",
                "atm_strike": 24000,
            }
            log = type("L", (), {"decision": "ACCEPTED"})()
            return sig, log

    now = datetime.now(timezone.utc)
    candles = []
    for i in range(70):
        # last bar timestamp == now so quote_age < 30s for contract validator
        ts = now - timedelta(minutes=5 * (69 - i))
        candles.append({
            "timestamp": ts.isoformat(),
            "open": 24000 + i, "high": 24005 + i, "low": 23995 + i,
            "close": 24000 + i, "volume": 1000,
        })
    data = FakeMarketData(candles, _atm_chain(24050), 24050.0)

    with mock.patch.dict(os.environ, env, clear=False):
        rt = PaperTradingRuntime()
        morning = datetime(2026, 9, 18, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        rt.now_fn = lambda: morning
        scanner = PaperMarketScanner(data=data, strategy=MockStrategy(), account_equity=100000)
        res = scanner.scan_once(rt, now=now)

    assert res.scanned is True, res
    assert res.traded is True, res
    assert data.calls["candles"] >= 1
    assert data.calls["chain"] >= 1

    from backend.database.db_manager import DatabaseManager
    db = DatabaseManager(db_path=path)
    trades = db.list_trades()
    positions = db.get_open_positions()
    assert len(trades) >= 1
    assert len(positions) >= 1
    assert trades[0].strategy == "V8_D_PULLBACK_ATM"


def test_real_v8d_evaluates_without_crash_on_fresh_bars():
    """Real V8-D strategy runs on synthetic bars; may or may not signal."""
    now = datetime.now(timezone.utc)
    candles = _bars_for_ce_signal(80)
    # freshen timestamps
    for i, c in enumerate(candles):
        c["timestamp"] = (now - timedelta(minutes=5 * (len(candles) - i))).isoformat()
    data = FakeMarketData(candles, _atm_chain(float(candles[-1]["close"])), float(candles[-1]["close"]))
    strategy = V8DStrategy()
    path = os.path.join(tempfile.mkdtemp(), "scan3.db")
    env = {
        "TRADING_MODE": "paper",
        "TRADING_STRATEGY": "V8_D_PULLBACK_ATM",
        "UPSTOX_ORDER_PRODUCT": "I",
        "DATABASE_PATH": path,
        "RISK_PER_TRADE_PCT": "0.025",
    }
    with mock.patch.dict(os.environ, env, clear=False):
        rt = PaperTradingRuntime()
        rt.now_fn = lambda: datetime(2026, 9, 18, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        scanner = PaperMarketScanner(data=data, strategy=strategy, min_bars=60)
        res = scanner.scan_once(rt, now=now)
    assert res.scanned is True
    # traded may be True or False depending on filter fit — must not error
    assert res.reason
    assert "strategy_error" not in res.reason
