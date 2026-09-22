"""Scanner must evaluate V8-D, not OPTION_PREMIUM, in paper mode."""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("TRADING_MODE", "paper")
os.environ["TRADING_STRATEGY"] = "V8_D_PULLBACK_ATM"
os.environ.setdefault("UPSTOX_ORDER_PRODUCT", "I")


def test_evaluate_configured_strategy_refuses_non_v8d_in_paper():
    os.environ["TRADING_STRATEGY"] = "OPTION_PREMIUM"
    os.environ["TRADING_MODE"] = "paper"
    import backend.config.settings as sm
    from backend.strategy import trading_engine as te
    te.settings = sm.load_settings()
    eng = te.TradingEngine(client=MagicMock())
    with patch("backend.config.settings.load_settings", return_value=sm.load_settings()):
        sig = eng.evaluate_configured_strategy("NIFTY50")
    joined = " ".join(sig.rejected_reasons)
    assert "Refusing silent OPTION_PREMIUM fallback" in joined or "requires TRADING_STRATEGY=V8_D" in joined
    os.environ["TRADING_STRATEGY"] = "V8_D_PULLBACK_ATM"


def test_scanner_uses_configured_strategy_not_option_premium():
    from backend.scanner.live_scanner import LiveScanner

    class Engine:
        def __init__(self):
            self.configured_calls = 0
            self.premium_calls = 0

        def evaluate_configured_strategy(self, symbol):
            self.configured_calls += 1
            return SimpleNamespace(
                strategy_name="V8_D_PULLBACK_ATM",
                signal="NONE",
                confidence=0.0,
                entry_reason="NO TRADE — Underlying technical pullback/reversal criteria not met",
                rejected_reasons=["Underlying technical pullback/reversal criteria not met"],
                indicators={},
                entry_price=None,
                to_dict=lambda: {"strategy_name": "V8_D_PULLBACK_ATM", "signal": "NONE"},
            )

        def evaluate_option_premium(self, symbol, **kw):
            self.premium_calls += 1
            raise AssertionError("OPTION_PREMIUM path must not be used")

    eng = Engine()
    scanner = LiveScanner(trading_engine=eng, universe_resolver=lambda: ["NIFTY50"])
    entry = scanner.scan_symbol("NIFTY50")
    assert eng.configured_calls == 1
    assert eng.premium_calls == 0
    assert entry.strategy_name == "V8_D_PULLBACK_ATM"
    assert entry.execution_status == "SIGNAL_ONLY"
