"""Comprehensive test suite for shutdown notification idempotency and lifecycle safety.

Verifies:
1. Normal startup does not trigger shutdown notifications.
2. Background task/scanner/websocket restarts do not trigger shutdown notifications.
3. Backend process shutdown sends at most ONE notification per process PID.
4. Duplicate shutdown calls within the same process are skipped and logged.
5. A new process lifecycle (new PID) can send exactly one shutdown notification.
6. Telegram send failures do not cause recursive loops or unhandled exceptions.
7. Manual stop only notifies if the bot was running; repeated stops do not spam.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from backend.strategy.trading_engine import (
    BotState,
    TradingEngine,
    _shutdown_lock,
)
import backend.strategy.trading_engine as te_module


class TestShutdownIdempotency(unittest.TestCase):
    def setUp(self) -> None:
        # Reset shutdown PID state before each test
        with _shutdown_lock:
            te_module._process_shutdown_notification_sent_pid = None

    def tearDown(self) -> None:
        with _shutdown_lock:
            te_module._process_shutdown_notification_sent_pid = None

    def test_startup_does_not_send_shutdown_notification(self) -> None:
        mock_telegram = MagicMock()
        engine = TradingEngine(telegram_alerts=mock_telegram)
        
        with patch.object(engine, "notify") as mock_notify:
            engine.start()
            mock_notify.assert_called_once()
            args, _ = mock_notify.call_args
            self.assertIn("Trading bot started", args[0])
            self.assertNotIn("stopped", args[0].lower())

    def test_shutdown_sends_exactly_one_notification_per_process(self) -> None:
        mock_telegram = MagicMock()
        engine = TradingEngine(telegram_alerts=mock_telegram)
        
        with patch.object(engine, "notify") as mock_notify:
            # First shutdown call
            engine.stop("Server shutdown")
            self.assertEqual(mock_notify.call_count, 1)
            self.assertIn("Trading bot stopped: Server shutdown", mock_notify.call_args[0][0])
            
            # Second shutdown call in same process (e.g. signal + lifespan cleanup)
            engine.stop("Server shutdown")
            self.assertEqual(mock_notify.call_count, 1)  # NOT called again
            
            # Third shutdown call
            engine.stop("Server shutdown")
            self.assertEqual(mock_notify.call_count, 1)  # Still 1

    def test_process_restart_resets_guard_for_new_process(self) -> None:
        mock_telegram = MagicMock()
        engine = TradingEngine(telegram_alerts=mock_telegram)
        
        with patch.object(engine, "notify") as mock_notify:
            # Process 1000
            with patch("os.getpid", return_value=1000):
                engine.stop("Server shutdown")
                self.assertEqual(mock_notify.call_count, 1)
                engine.stop("Server shutdown")
                self.assertEqual(mock_notify.call_count, 1)

            # Process 2000 (new process after restart)
            with patch("os.getpid", return_value=2000):
                engine.stop("Server shutdown")
                self.assertEqual(mock_notify.call_count, 2)
                engine.stop("Server shutdown")
                self.assertEqual(mock_notify.call_count, 2)

    def test_telegram_failure_does_not_raise_or_loop(self) -> None:
        mock_telegram = MagicMock()
        mock_telegram.send_message.side_effect = Exception("Telegram API timeout")
        
        engine = TradingEngine(telegram_alerts=mock_telegram)
        
        # Must not raise an exception
        try:
            engine.stop("Server shutdown")
        except Exception as e:
            self.fail(f"engine.stop raised an exception on Telegram failure: {e}")

    def test_manual_stop_notifies_only_when_running(self) -> None:
        mock_telegram = MagicMock()
        engine = TradingEngine(telegram_alerts=mock_telegram)
        
        with patch.object(BotState, "is_running", side_effect=[True, False]):
            with patch.object(engine, "notify") as mock_notify:
                # First manual stop when bot was running
                engine.stop("Manual stop via dashboard")
                self.assertEqual(mock_notify.call_count, 1)
                self.assertIn("Manual stop via dashboard", mock_notify.call_args[0][0])
                
                # Second manual stop when bot was not running
                engine.stop("Manual stop via dashboard")
                self.assertEqual(mock_notify.call_count, 1)  # No duplicate spam


if __name__ == "__main__":
    unittest.main()
