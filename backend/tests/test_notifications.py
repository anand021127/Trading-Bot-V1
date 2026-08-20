"""Tests for notification helper classes."""

from __future__ import annotations

from unittest.mock import patch

from backend.notifications.email_alerts import EmailAlerts
from backend.notifications.telegram_alerts import TelegramAlerts


def test_telegram_send_message_requires_credentials() -> None:
    with patch.dict("os.environ", {}, clear=True):
        alerts = TelegramAlerts()
        assert alerts.send_message("Hi") is False


def test_email_send_requires_credentials() -> None:
    with patch.dict("os.environ", {}, clear=True):
        alerts = EmailAlerts()
        assert alerts.send_email("Subject", "Body") is False
