"""PHASE B — Strategy context + V8-D freeze + scope-guard tests.

- build_strategy_context() must keep reflecting the REAL V8DStrategy class
  parameters (pinned consistency) and report the authoritative runtime
  capital/limits from the unified resolver.
- V8-D strategy file must remain BYTE-IDENTICAL (EOL-normalized sha256
  d468cc110401e3b2…).
- Email/SMTP must remain absent.
"""
from __future__ import annotations

import hashlib
import os

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

V8D_SHA256_PREFIX = "d468cc110401e3b2"


def _eol_normalized_sha256(path: str) -> str:
    with open(path, "rb") as fh:
        raw = fh.read()
    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def test_v8d_strategy_file_frozen():
    path = os.path.join(REPO_ROOT, "backend", "strategy", "strategies", "v8d_strategy.py")
    assert os.path.exists(path)
    digest = _eol_normalized_sha256(path)
    assert digest.startswith(V8D_SHA256_PREFIX), (
        f"V8-D FROZEN FILE MODIFIED: sha256={digest} (expected {V8D_SHA256_PREFIX}…). "
        "Phase B must not change strategy parameters/logic."
    )


def test_strategy_context_matches_v8d_class():
    from backend.copilot.strategy_context import build_strategy_context, V8D_PARAMS
    from backend.strategy.strategies.v8d_strategy import V8DStrategy
    s = V8DStrategy()
    ctx = build_strategy_context()
    params = ctx["strategy"]["parameters"]
    for key in V8D_PARAMS:
        expected = getattr(s, key, V8D_PARAMS[key])
        assert params[key] == expected, f"{key} drifted: {params[key]} != {expected}"
    assert ctx["strategy"]["name"] == "V8_D_PULLBACK_ATM"
    assert ctx["strategy"]["entry_policy"]["last_entry_ist"] == "14:45"


def test_strategy_context_reflects_authoritative_risk(monkeypatch, tmp_path):
    """The strategy context's risk_state must surface the AUTHORITATIVE
    max_daily_trades (saved blob over env), not a hardcoded 3."""
    from backend.database.db_manager import DatabaseManager
    db = DatabaseManager(db_path=str(tmp_path / "s.sqlite"))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "s.sqlite"))
    db.save_settings_blob({
        "mode": "paper",
        "capital": {"total": 20000},
        "risk": {"max_trades_per_day": 20},
    })
    from backend.config import runtime_config
    runtime_config.set_runtime_config_db(db)
    runtime_config.invalidate_runtime_config_cache()
    from backend.copilot.strategy_context import build_strategy_context
    ctx = build_strategy_context()
    assert ctx["risk_state"]["max_daily_trades"] == 20
    assert ctx["risk_state"]["starting_capital"] == 20000.0
    assert ctx["risk_state"]["config_source"] == "sqlite_settings_over_env"
    runtime_config.invalidate_runtime_config_cache()
    runtime_config.set_runtime_config_db(None)


def test_email_alerts_absent():
    """Email alerts stay removed: no SMTP/email modules or wiring."""
    backend_root = os.path.join(REPO_ROOT, "backend")
    banned_files = ("email_alerts.py", "smtp_alerts.py")
    for root, _dirs, files in os.walk(backend_root):
        if "__pycache__" in root:
            continue
        for f in files:
            if f.endswith(".py") and f in banned_files:
                raise AssertionError(f"email alert file reappeared: {f}")
    import glob
    for py in glob.glob(os.path.join(backend_root, "**", "*.py"), recursive=True):
        if "__pycache__" in py:
            continue
        # Test files legitimately mention smtplib inside NOT-contains
        # assertions; only production code must be free of it.
        if os.sep + "tests" + os.sep in py or "/tests/" in py.replace("\\", "/"):
            continue
        with open(py, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        low = src.lower()
        assert "import smtplib" not in low, f"SMTP import found in {py}"
        assert "smtp.gmail" not in low, f"SMTP host found in {py}"


def test_copilot_context_exposes_guardrails():
    from backend.copilot.strategy_context import build_strategy_context
    ctx = build_strategy_context()
    assert ctx["copilot_guardrails"]
    assert any("read-only" in g.lower() for g in ctx["copilot_guardrails"])
