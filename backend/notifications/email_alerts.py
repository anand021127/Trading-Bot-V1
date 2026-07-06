"""Email alerting via SMTP Gmail."""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from typing import Dict, Optional


class EmailAlerts:
    def __init__(self, smtp_server: Optional[str] = None, smtp_port: Optional[int] = None) -> None:
        self.smtp_server = smtp_server or os.getenv("SMTP_SERVER", "smtp.gmail.com")
        self.smtp_port = smtp_port or int(os.getenv("SMTP_PORT", "587"))
        self.sender_email = os.getenv("SENDER_EMAIL")
        self.recipient_email = os.getenv("RECIPIENT_EMAIL")
        self.password = os.getenv("EMAIL_PASSWORD")

    def _validate(self) -> None:
        if not self.sender_email or not self.recipient_email or not self.password:
            raise ValueError("Email sender, recipient, and password are required")

    def send_email(self, subject: str, body: str) -> None:
        self._validate()
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.sender_email
        message["To"] = self.recipient_email
        message.set_content(body)

        with smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(self.sender_email, self.password)
            smtp.send_message(message)
