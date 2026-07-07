"""Telegram notifications — production grade with error handling."""
from __future__ import annotations

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class TelegramAlerts:
    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> None:
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id   = chat_id   or os.getenv("TELEGRAM_CHAT_ID",   "")

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send_message(self, message: str) -> bool:
        """Send a message. Returns True on success, False on failure."""
        if not self.is_configured:
            logger.debug("Telegram not configured — message skipped")
            return False
        try:
            url  = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            resp = requests.post(
                url,
                json={"chat_id": self.chat_id, "text": message},
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.warning("Telegram send failed: %s", e)
            return False

    def send_trade_entry(self, symbol: str, entry: float, sl: float, qty: int, conf: float, mode: str) -> None:
        icon = "📝" if mode == "paper" else "🟢"
        self.send_message(
            f"{icon} ENTRY [{mode.upper()}]\n"
            f"Symbol: {symbol}\n"
            f"Entry: ₹{entry:.2f} | SL: ₹{sl:.2f}\n"
            f"Qty: {qty} | Confidence: {conf*100:.0f}%"
        )

    def send_trade_exit(self, symbol: str, exit_p: float, net_pnl: float, reason: str, mode: str) -> None:
        icon = "✅" if net_pnl >= 0 else "❌"
        self.send_message(
            f"{icon} EXIT [{mode.upper()}]\n"
            f"Symbol: {symbol}\n"
            f"Exit: ₹{exit_p:.2f} | Net PnL: ₹{net_pnl:.0f}\n"
            f"Reason: {reason}"
        )

    def send_daily_summary(self, date: str, trades: int, wins: int, net_pnl: float, mode: str) -> None:
        self.send_message(
            f"📊 DAILY SUMMARY [{date}] [{mode.upper()}]\n"
            f"Trades: {trades} | Wins: {wins} | Losses: {trades-wins}\n"
            f"Net PnL: ₹{net_pnl:.0f}\n"
            f"Win Rate: {wins/trades*100:.1f}%" if trades else f"No trades today."
        )

    def send_risk_alert(self, alert_type: str, details: str) -> None:
        self.send_message(f"⚠️ RISK ALERT: {alert_type}\n{details}")
