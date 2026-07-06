"""Tests for notification helper classes."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.notifications.email_alerts import EmailAlerts
from backend.notifications.telegram_alerts import TelegramAlerts


def test_telegram_send_message_requires_credentials() -> None:
    with patch.dict("os.environ", {}, clear=True):
        alerts = TelegramAlerts()
        with pytest.raises(ValueError):
            alerts.send_message("Hi")


def test_email_send_requires_credentials() -> None:
    with patch.dict("os.environ", {}, clear=True):
        alerts = EmailAlerts()
        with pytest.raises(ValueError):
            alerts.send_email("Subject", "Body")
