"""Regression tests: email/SMTP alert functionality fully removed.

The product is Telegram-only for push notifications. These tests pin the
removal so the email code path cannot silently return: no EmailAlerts
module, no SMTP/smtplib use anywhere in backend/, no email test in the
diagnostics TEST_MAP, and the /api/alerts/test endpoint refusing the
"email" channel.
"""
from __future__ import annotations

import ast
import builtins
import os
from pathlib import Path

os.environ.setdefault("TRADING_BOT_OFFLINE_TESTS", "1")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.api.main import app  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent


def test_email_alerts_module_is_gone() -> None:
    assert not (BACKEND_DIR / "notifications" / "email_alerts.py").exists(), (
        "backend/notifications/email_alerts.py must not exist — email alerts were removed"
    )


def test_no_smtp_imports_anywhere_in_backend() -> None:
    """No backend application module may import smtplib or email MIME.
    (Test files are excluded — this test itself uses smtplib as a spy.)"""
    offenders: list[str] = []
    for py in BACKEND_DIR.rglob("*.py"):
        if "__pycache__" in py.parts or "tests" in py.parts:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(n == "smtplib" or n.startswith("smtplib.") or n.startswith("email.mime") for n in names):
                offenders.append(str(py.relative_to(REPO_ROOT)))
    assert offenders == [], f"SMTP imports found in: {offenders}"


def test_no_smtp_env_vars_read_in_backend() -> None:
    banned = ("SMTP_SERVER", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
              "EMAIL_PASSWORD", "SENDER_EMAIL", "RECIPIENT_EMAIL")
    offenders: list[str] = []
    for py in BACKEND_DIR.rglob("*.py"):
        if "__pycache__" in py.parts or "tests" in py.parts:
            continue
        text = py.read_text(encoding="utf-8", errors="replace")
        if any(v in text for v in banned):
            offenders.append(str(py.relative_to(REPO_ROOT)))
    assert offenders == [], f"email/SMTP env vars still referenced in: {offenders}"


def test_diagnostics_test_map_has_no_email_entry() -> None:
    from backend.api.routers.diagnostics import TEST_MAP

    assert "email" not in TEST_MAP
    assert "smtp" not in TEST_MAP
    assert "live_quote" in TEST_MAP  # supported feature must remain


def test_alerts_test_endpoint_refuses_email_channel() -> None:
    client = TestClient(app)
    response = client.post("/api/alerts/test?channel=email")
    assert response.status_code == 400
    assert response.json()["detail"] == "unsupported channel"


def test_alerts_status_has_no_email_keys() -> None:
    client = TestClient(app)
    response = client.get("/api/alerts/")
    assert response.status_code == 200
    body = response.json()
    assert "email_enabled" not in body
    assert "smtp_server" not in body


def test_smtp_never_called_when_sending_test_alerts() -> None:
    """Hard runtime guarantee: even forced through every supported channel,
    no code path may construct an SMTP connection."""
    import smtplib

    calls: list[str] = []

    real_init = smtplib.SMTP.__init__

    def _spy(self, *args, **kwargs):  # noqa: ANN002, ANN003
        calls.append(str(args))
        return real_init(self, *args, **kwargs)

    client = TestClient(app)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(smtplib.SMTP, "__init__", _spy)
        client.post("/api/alerts/test?channel=telegram")   # disabled → 400, no SMTP
        client.post("/api/alerts/test?channel=email")      # removed → 400, no SMTP
        client.get("/api/alerts/")                          # status read, no SMTP

    assert calls == [], f"SMTP connection attempted: {calls}"
