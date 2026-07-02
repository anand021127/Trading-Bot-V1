"""Telegram notifications for bot alerts."""

from __future__ import annotations

import os
from typing import Optional

import requests


class TelegramAlerts:
    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None) -> None:
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")

    def _validate(self) -> None:
        if not self.bot_token or not self.chat_id:
            raise ValueError("Telegram bot token and chat id are required")

    def send_message(self, message: str) -> dict:
        self._validate()
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": message}
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return response.json()
