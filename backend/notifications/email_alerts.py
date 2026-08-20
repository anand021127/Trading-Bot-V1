"""Email notifications via Gmail SMTP."""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from typing import Optional

logger = logging.getLogger(__name__)


class EmailAlerts:
    def __init__(
        self,
        smtp_server: Optional[str] = None,
        smtp_port: Optional[int] = None,
    ) -> None:
        self.smtp_server    = smtp_server or os.getenv("SMTP_SERVER",    "smtp.gmail.com")
        self.smtp_port      = smtp_port   or int(os.getenv("SMTP_PORT",  "587"))
        self.sender_email   = os.getenv("SENDER_EMAIL",    "") or os.getenv("NOTIFICATION_EMAIL", "")
        self.recipient_email = os.getenv("RECIPIENT_EMAIL", "") or os.getenv("NOTIFICATION_EMAIL", "")
        self.password       = os.getenv("EMAIL_PASSWORD", "")

    @property
    def is_configured(self) -> bool:
        return bool(self.sender_email and self.recipient_email and self.password)

    def send_email(self, subject: str, body: str) -> bool:
        if not self.is_configured:
            logger.debug("Email not configured — skipped")
            return False
        try:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"]    = self.sender_email
            msg["To"]      = self.recipient_email
            msg.set_content(body)
            with smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=15) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.login(self.sender_email, self.password)
                smtp.send_message(msg)
            return True
        except Exception as e:
            logger.warning("Email send failed: %s", e)
            return False
