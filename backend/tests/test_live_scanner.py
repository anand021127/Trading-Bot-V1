"""Tests for the live scanner (item #3)."""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock

from backend.scanner.live_scanner import LiveScanner
from backend.strategy.signal import StrategySignal, SignalType


def _ema_signal(symbol: str, all_pass: bool) -> StrategySignal:
    sig = StrategySignal(strategy_name="EMA_TREND", symbol=symbol)
    sig.conditions = {
        "ema_trend_up": all_pass,
        "price_above_ema20": all_pass,
        "rsi_in_range": all_pass,
        "volume_confirmed": False if not all_pass else True,
    }
    sig.indicators = {"rsi": 62.5, "atr": 1.2, "ema_fast": 101, "ema_slow": 99}
    sig.confidence = 100.0 if all_pass else 50.0
    if all_pass:
        sig.signal = SignalType.BUY
        sig.entry_price = 105.0
        sig.entry_reason = "ALL CONDITIONS PASSED — BUY SIGNAL GENERATED."
    else:
        sig.rejected_reasons = ["Volume did not confirm"]
        sig.entry_reason = "NO TRADE — VOLUME FAILED"
    return sig


def _orb_signal(symbol: str) -> StrategySignal:
    sig = StrategySignal(strategy_name="ORB", symbol=symbol)
    sig.conditions = {"orb_captured": True, "price_above_orb_high": False, "volume_breakout": False}
    sig.rejected_reasons = ["Price has not broken above the opening range high"]
    sig.entry_reason = "NO TRADE — ORB_CAPTURED PASS, PRICE_ABOVE_ORB_HIGH FAILED, VOLUME_BREAKOUT FAILED"
    sig.confidence = 33.3
    return sig


def _make_engine(signals_by_symbol: Dict[str, List[StrategySignal]]) -> Any:
    engine = MagicMock()
    engine.evaluate_all_strategies.side_effect = lambda symbol, **kw: signals_by_symbol[symbol]
    return engine


class TestLiveScanner:
    def test_scan_symbol_all_pass_shows_buy_decision(self) -> None:
        engine = _make_engine({"HDFCBANK": [_ema_signal("HDFCBANK", True), _orb_signal("HDFCBANK")]})
        scanner = LiveScanner(trading_engine=engine, universe_resolver=lambda: ["HDFCBANK"])

        entry = scanner.scan_symbol("HDFCBANK")

        assert entry.symbol == "HDFCBANK"
        assert entry.ema_status == "PASS"
        assert entry.rsi_status == "PASS"
        assert entry.signal == SignalType.BUY
        assert "BUY SIGNAL GENERATED" in entry.decision

    def test_scan_symbol_rejected_shows_reasons_never_hidden(self) -> None:
        engine = _make_engine({"RELIANCE": [_ema_signal("RELIANCE", False), _orb_signal("RELIANCE")]})
        scanner = LiveScanner(trading_engine=engine, universe_resolver=lambda: ["RELIANCE"])

        entry = scanner.scan_symbol("RELIANCE")

        assert entry.signal == SignalType.NONE
        assert entry.volume_status == "FAILED"
        assert len(entry.rejected_reasons) >= 1
        assert "NO TRADE" in entry.decision

    def test_scan_symbol_handles_engine_error_explicitly(self) -> None:
        engine = MagicMock()
        engine.evaluate_all_strategies.side_effect = RuntimeError("Upstox API 403")
        scanner = LiveScanner(trading_engine=engine, universe_resolver=lambda: ["TCS"])

        entry = scanner.scan_symbol("TCS")

        assert entry.error is not None
        assert "403" in entry.error
        assert "ERROR" in entry.decision

    def test_scan_once_iterates_full_universe(self) -> None:
        engine = _make_engine({
            "RELIANCE": [_ema_signal("RELIANCE", False)],
            "HDFCBANK": [_ema_signal("HDFCBANK", True)],
        })
        scanner = LiveScanner(
            trading_engine=engine, universe_resolver=lambda: ["RELIANCE", "HDFCBANK"],
        )
        results = scanner.scan_once()
        assert [r.symbol for r in results] == ["RELIANCE", "HDFCBANK"]
        assert scanner.currently_scanning is None  # reset after full pass
        assert scanner.last_full_pass_completed_at is not None

    def test_status_report_shape(self) -> None:
        engine = _make_engine({"HDFCBANK": [_ema_signal("HDFCBANK", True)]})
        scanner = LiveScanner(trading_engine=engine, universe_resolver=lambda: ["HDFCBANK"])
        scanner.scan_once()
        report = scanner.status_report()
        assert report["watching_count"] == 1
        assert report["is_running"] is False  # not started via run_forever()
        assert len(report["results"]) == 1

    def test_get_result_returns_none_for_unscanned_symbol(self) -> None:
        scanner = LiveScanner(trading_engine=MagicMock(), universe_resolver=lambda: [])
        assert scanner.get_result("NOPE") is None


class TestLiveScannerOptionsMode:
    def test_options_mode_calls_evaluate_option_premium_not_evaluate_all_strategies(self) -> None:
        engine = MagicMock()
        option_sig = StrategySignal(strategy_name="OPTION_PREMIUM", symbol="NIFTY50",
                                     signal=SignalType.BUY, confidence=90.0, entry_price=145.5)
        option_sig.indicators = {"selected_contract": {"option_type": "CE", "strike": 22000}}
        engine.evaluate_option_premium.return_value = option_sig

        scanner = LiveScanner(
            trading_engine=engine, universe_resolver=lambda: ["NIFTY50"],
            mode_resolver=lambda: "OPTIONS",
        )
        entry = scanner.scan_symbol("NIFTY50")

        engine.evaluate_option_premium.assert_called_once_with("NIFTY50")
        engine.evaluate_all_strategies.assert_not_called()
        assert entry.signal == SignalType.BUY
        assert entry.ltp == 145.5
        assert entry.trend == "BULLISH"  # CE selected

    def test_stocks_mode_still_uses_evaluate_all_strategies(self) -> None:
        engine = _make_engine({"RELIANCE": [_ema_signal("RELIANCE", True)]})
        scanner = LiveScanner(
            trading_engine=engine, universe_resolver=lambda: ["RELIANCE"],
            mode_resolver=lambda: "STOCKS",
        )
        scanner.scan_symbol("RELIANCE")
        engine.evaluate_all_strategies.assert_called_once()

    def test_default_mode_resolver_is_stocks_when_not_supplied(self) -> None:
        engine = _make_engine({"RELIANCE": [_ema_signal("RELIANCE", True)]})
        scanner = LiveScanner(trading_engine=engine, universe_resolver=lambda: ["RELIANCE"])
        scanner.scan_symbol("RELIANCE")
        engine.evaluate_all_strategies.assert_called_once()
        engine.evaluate_option_premium.assert_not_called()

    def test_options_mode_error_is_shown_honestly(self) -> None:
        engine = MagicMock()
        engine.evaluate_option_premium.side_effect = RuntimeError("no option chain access")
        scanner = LiveScanner(
            trading_engine=engine, universe_resolver=lambda: ["SENSEX"],
            mode_resolver=lambda: "OPTIONS",
        )
        entry = scanner.scan_symbol("SENSEX")
        assert entry.error is not None
        assert "no option chain access" in entry.error
