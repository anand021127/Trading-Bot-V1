"""Regression tests for the full-system recheck (security, wiring, setup, crashes)."""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from backend.api.main import app
from backend.api.routers import upstox_v3_auth as _auth_mod   # the module object the app's routes use
from backend.market.calendar import CalendarDataError, SessionCalendar

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "s3cr3t-test-token"

# State-changing endpoints that are deliberately NOT behind CONTROL_TOKEN:
#   * Upstox calls the two webhooks itself (hardened separately: client_id + optional secret);
#   * Copilot chat/trade-plan are read-only explanation endpoints (no bot/trading state changes).
EXPECTED_OPEN = {
    ("POST", "/webhooks/upstox-token-notifier"),
    ("POST", "/api/webhooks/upstox-token-notifier"),
    ("POST", "/api/copilot/chat/submit"),
    ("POST", "/api/copilot/chat/status/{job_id}/cancel"),
    ("POST", "/api/copilot/chat"),
    ("POST", "/api/copilot/trade-plan"),
}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", os.path.join(tempfile.mkdtemp(), "recheck.db"))
    return TestClient(app, raise_server_exceptions=False)


def _mutating():
    for path, ops in app.openapi()["paths"].items():
        for meth in ("post", "put", "patch", "delete"):
            if meth in ops:
                yield meth.upper(), path


# ── control-token coverage ──────────────────────────────────────────────────
def test_every_state_changing_endpoint_requires_the_control_token(client, monkeypatch):
    monkeypatch.setenv("CONTROL_TOKEN", TOKEN)
    open_routes = set()
    for meth, path in _mutating():
        r = client.request(meth, re.sub(r"\{[^}]+\}", "x", path), json={})
        if r.status_code != 401:
            open_routes.add((meth, path))
    assert open_routes == EXPECTED_OPEN, (
        f"unexpectedly OPEN: {open_routes - EXPECTED_OPEN}; unexpectedly closed: {EXPECTED_OPEN - open_routes}")


@pytest.mark.parametrize("meth,url", [
    ("PUT", "/api/universe/"), ("POST", "/api/diagnostics/test/nope"), ("POST", "/api/diagnostics/run-all"),
    ("POST", "/api/diagnostics/instrument-master/refresh"), ("POST", "/api/alerts/test"),
    ("POST", "/api/scanner/scan-now"), ("POST", "/api/upstox/auth/request"),
])
def test_newly_protected_routes_accept_the_right_token_and_reject_the_wrong_one(client, monkeypatch, meth, url):
    monkeypatch.setenv("CONTROL_TOKEN", TOKEN)
    assert client.request(meth, url, json={}).status_code == 401
    assert client.request(meth, url, json={}, headers={"X-Control-Token": "wrong"}).status_code == 401
    if url in ("/api/diagnostics/run-all", "/api/diagnostics/instrument-master/refresh"):
        return                                     # authorised path would run real checks; 401 behaviour is the point
    assert client.request(meth, url, json={}, headers={"X-Control-Token": TOKEN}).status_code != 401
    assert client.request(meth, url, json={}, headers={"Authorization": f"Bearer {TOKEN}"}).status_code != 401


def test_without_a_configured_token_behaviour_is_unchanged(client, monkeypatch):
    monkeypatch.delenv("CONTROL_TOKEN", raising=False)
    assert client.put("/api/universe/", json={}).status_code != 401        # documented: open when unset (local/paper)


# ── Upstox token webhook ────────────────────────────────────────────────────
def _hook(client, body, **kw):
    return client.post("/api/webhooks/upstox-token-notifier", json=body, **kw)


GOOD = {"message_type": "access_token", "client_id": "app-123", "user_id": "u1", "access_token": "tok-abcdef-123456"}


def test_webhook_requires_matching_client_id_when_one_is_configured(client, monkeypatch):
    monkeypatch.delenv("UPSTOX_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "original")             # the handler overwrites this; monkeypatch restores it
    with mock.patch.object(_auth_mod, "_get_client_credentials", return_value=("app-123", "sec")), \
         mock.patch.object(_auth_mod, "_db") as db:
        no_id = {k: v for k, v in GOOD.items() if k != "client_id"}
        assert _hook(client, no_id).status_code == 403                    # used to be ACCEPTED (check skipped)
        assert _hook(client, {**GOOD, "client_id": ""}).status_code == 403
        assert _hook(client, {**GOOD, "client_id": "other-app"}).status_code == 403
        assert os.environ["UPSTOX_ACCESS_TOKEN"] == "original"            # nothing was stored
        db.save_token.assert_not_called()
        assert _hook(client, GOOD).status_code == 200
        db.save_token.assert_called_once_with("tok-abcdef-123456")


def test_webhook_optional_shared_secret(client, monkeypatch):
    monkeypatch.setenv("UPSTOX_WEBHOOK_SECRET", "hook-secret")
    monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "original")
    with mock.patch.object(_auth_mod, "_get_client_credentials", return_value=("app-123", "sec")), \
         mock.patch.object(_auth_mod, "_db") as db:
        assert _hook(client, GOOD).status_code == 401
        assert _hook(client, GOOD, params={"secret": "nope"}).status_code == 401
        assert _hook(client, GOOD, headers={"X-Webhook-Secret": "nope"}).status_code == 401
        db.save_token.assert_not_called()
        assert _hook(client, GOOD, params={"secret": "hook-secret"}).status_code == 200
        assert _hook(client, GOOD, headers={"X-Webhook-Secret": "hook-secret"}).status_code == 200


# ── holiday calendar ────────────────────────────────────────────────────────
def _clean_holiday_env(monkeypatch):
    for k in [k for k in os.environ if k.startswith("EXCHANGE_HOLIDAYS_")]:
        monkeypatch.delenv(k)


def test_calendar_operator_override_is_additive_validated_and_fails_loudly(monkeypatch):
    _clean_holiday_env(monkeypatch)
    base = SessionCalendar()
    assert base.verified_through_year() == 2026
    with pytest.raises(CalendarDataError):
        base.is_holiday("2027-01-27")                                   # still fails CLOSED without data
    monkeypatch.setenv("EXCHANGE_HOLIDAYS_2027", "2027-01-26, 2027-03-04")
    cal = SessionCalendar()
    assert cal.verified_through_year() == 2027
    assert cal.is_holiday("2027-01-26") and not cal.is_holiday("2027-01-27") and cal.is_trading_day("2027-01-27")
    builtin_2026 = sorted(base._holidays[2026])
    monkeypatch.setenv("EXCHANGE_HOLIDAYS_2026", "2026-12-24")           # additive: can add, NEVER remove
    cal2 = SessionCalendar()
    assert set(builtin_2026) <= set(cal2._holidays[2026]) and "2026-12-24" in cal2._holidays[2026]
    for bad in ("2027-13-45", "26-01-2027", "2028-01-26"):
        monkeypatch.setenv("EXCHANGE_HOLIDAYS_2027", bad)
        with pytest.raises(CalendarDataError):
            SessionCalendar()


def test_calendar_warns_from_october_and_errors_when_the_year_is_missing(monkeypatch):
    _clean_holiday_env(monkeypatch)
    cal = SessionCalendar()
    assert cal.coverage_info(date(2026, 6, 1))["severity"] == "ok"
    w = cal.coverage_info(date(2026, 10, 3))
    assert w["severity"] == "warning" and "EXCHANGE_HOLIDAYS_2027" in w["warning"]
    e = cal.coverage_info(date(2027, 1, 4))
    assert e["severity"] == "error" and "will not trade" in e["warning"]
    monkeypatch.setenv("EXCHANGE_HOLIDAYS_2027", "2027-01-26")
    assert SessionCalendar().coverage_info(date(2026, 10, 3))["severity"] == "ok"


def test_health_and_copilot_surface_the_calendar_warning(client):
    with mock.patch("backend.market.calendar.exchange_calendar.coverage_info",
                    return_value={"verified_through_year": 2026, "severity": "warning", "warning": "table ends 2026"}):
        assert client.get("/health").json()["calendar"]["warning"] == "table ends 2026"
        from backend.copilot.full_context import _mismatches
        assert any(m["key"] == "exchange_holidays" and "table ends 2026" in m["message"] for m in _mismatches())


def _no_calendar():
    from backend.market import calendar as cal
    return mock.patch.object(cal.exchange_calendar, "market_is_open",
                             side_effect=cal.CalendarDataError("No verified exchange holiday table for 2027."))


def test_strict_calendar_path_still_fails_closed_for_trading():
    from backend.market import calendar as cal
    with _no_calendar():
        with pytest.raises(cal.CalendarDataError):
            cal.is_market_open_now()                 # order/scan paths must NOT guess


def test_display_paths_report_closed_instead_of_crashing_when_the_table_is_missing(client):
    from backend.market import calendar as cal
    with _no_calendar():
        assert cal.is_market_open_now_safe() is False
        for url in ("/api/overview", "/api/prices/live", "/api/prices/underlyings"):
            r = client.get(url)
            assert r.status_code == 200, (url, r.status_code)
        assert client.get("/api/overview").json()["system"]["market_open"] is False
        from backend.api import websocket as ws
        assert ws._is_market_open() is False


def test_test_suite_is_independent_of_the_holiday_table_year():
    assert os.environ.get("EXCHANGE_HOLIDAYS_2027") and os.environ.get("EXCHANGE_HOLIDAYS_2028")
    from backend.market.calendar import exchange_calendar
    assert exchange_calendar.verified_through_year() >= 2029      # test-process stand-in (see conftest)


# ── crashes found by static analysis / smoke test ───────────────────────────
def test_ai_status_endpoint_no_longer_500s(client):
    r = client.get("/api/ai/status")
    assert r.status_code == 200 and {"ai_enabled", "ai_mode"} <= set(r.json())
    assert "min_confidence" not in r.json()                              # vestigial fields that did not exist


def test_gate_chain_handles_a_no_signal_record_without_v8d_diagnostics():
    """NameError regression: `detail` was referenced inside _human_summary."""
    from backend.copilot.gate_chain import build_gate_chain_from_db
    from backend.database.db_manager import DatabaseManager
    from backend.paper import scan_state as ss
    db = DatabaseManager(db_path=os.path.join(tempfile.mkdtemp(), "gc.db"))
    rec = ss.build_scan_record(seq=1, scanned=True, traded=False, reason="no_trade:NO_SIGNAL",
                               details={"rejection": ["Underlying technical pullback/reversal criteria not met"]},
                               strategy="V8_D_PULLBACK_ATM", underlying="NIFTY50")
    ss.persist_scan_record(db, rec)
    chain = build_gate_chain_from_db(db)
    assert chain["stage"] == "NO_SIGNAL" and "NO SIGNAL" in chain["human_summary"].upper()


def test_token_status_endpoint_has_no_unreachable_code_or_undefined_names():
    import ast
    tree = ast.parse((ROOT / "backend/api/routers/settings.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name.startswith("token_status"))
    assert not any(isinstance(n, ast.Return) and i < len(fn.body) - 1 for i, n in enumerate(fn.body))


def test_every_parameterless_get_endpoint_answers_without_a_server_error(client):
    """Smoke test over the whole API surface (the 503 is the documented 'scanner not created
    under the offline-test flag')."""
    gets = sorted(p for p, v in app.openapi()["paths"].items() if "get" in v and "{" not in p)
    assert len(gets) >= 45
    bad = []
    for p in gets:
        r = client.get(p, follow_redirects=True)
        if r.status_code >= 500 and p != "/api/scanner/status":
            bad.append((p, r.status_code, r.text[:100]))
    assert not bad, bad


# ── configuration / setup documentation ─────────────────────────────────────
def test_env_example_documents_the_settings_that_were_missing():
    t = (ROOT / ".env.example").read_text(encoding="utf-8")
    for k in ("CONTROL_TOKEN", "UPSTOX_WEBHOOK_SECRET", "EXCHANGE_HOLIDAYS_2027", "COPILOT_LLM_BACKEND",
              "COPILOT_LLM_BASE_URL", "COPILOT_LLM_MODEL", "COPILOT_LLM_TIMEOUT_SECONDS", "PAPER_MAX_CANDLE_AGE_SEC",
              "PAPER_UNDERLYINGS", "VITE_BACKEND_URL", "TRADING_CAPITAL"):
        assert k in t, k
    from backend.copilot.config import load_copilot_settings
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("COPILOT_LLM_TIMEOUT_SECONDS", None)
        assert load_copilot_settings().llm_timeout_seconds == 45.0        # was 8 s: too short for a local CPU model


def test_render_blueprint_persists_the_database_and_pins_paper_mode():
    r = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "key: DATABASE_PATH" in r and "/data/" in r.split("key: DATABASE_PATH")[1].split("\n")[1]
    assert "key: TRADING_MODE" in r and "value: paper" in r.split("key: TRADING_MODE")[1].split("\n")[1]
    assert "V8_D_PULLBACK_ATM" in r
    assert "in-process" not in r.lower() or "separate" in r.lower()      # stale single-process claim removed


def test_both_vercel_configs_stay_identical_and_install_reproducibly():
    a = (ROOT / "vercel.json").read_text(encoding="utf-8")
    b = (ROOT / "frontend/vercel.json").read_text(encoding="utf-8")
    assert a == b                                              # root and frontend deployments must not drift
    assert json.loads(a)["installCommand"] == "npm ci"         # lockfile-exact installs


# ── frontend wiring (static) ────────────────────────────────────────────────
def test_websocket_url_is_derived_in_exactly_one_place_from_the_rest_base():
    fe = ROOT / "frontend/src"
    defs = [p for p in fe.rglob("*.ts*") if ".test." not in p.name and
            re.search(r"function buildWsUrl\s*\(", p.read_text(encoding="utf-8"))]
    assert [p.name for p in defs] == ["client.ts"], defs
    for f in ("hooks/useWebSocket.ts", "context/ConnectionContext.tsx"):
        t = (fe / f).read_text(encoding="utf-8")
        assert "buildWsUrl" in t and "from '../api/client'" in t


def test_frontend_sends_the_control_token_and_does_not_mistake_it_for_an_upstox_expiry():
    c = (ROOT / "frontend/src/api/client.ts").read_text(encoding="utf-8")
    assert "X-Control-Token" in c and "interceptors.request.use" in c
    assert "isControlTokenError" in c and "control:token_required" in c
    assert "ControlTokenCard" in (ROOT / "frontend/src/pages/Settings.tsx").read_text(encoding="utf-8")
    assert "control:token_required" in (ROOT / "frontend/src/components/Layout.tsx").read_text(encoding="utf-8")


def test_dead_frontend_endpoint_for_a_nonexistent_route_is_gone():
    assert "/api/prices/nifty50" not in (ROOT / "frontend/src/api/endpoints.ts").read_text(encoding="utf-8")


# ── the displayed daily-trade limit must be the one that actually binds ─────
def test_effective_daily_cap_is_the_smaller_of_the_risk_limit_and_the_strategy_cap():
    from backend.strategy.strategies.v8d_strategy import V8DStrategy
    from backend.strategy.v8d_diagnostics import daily_trade_caps
    cap = V8DStrategy().max_daily_trades
    assert daily_trade_caps(20) == {"configured_max_trades": 20, "strategy_daily_cap": cap, "effective_max_trades": cap}
    assert daily_trade_caps(2)["effective_max_trades"] == 2                       # a tighter risk limit still wins
    assert daily_trade_caps(None)["effective_max_trades"] == cap


def test_api_reports_all_three_numbers_but_leaves_the_risk_limit_untouched(client):
    d = client.get("/api/overview").json()["risk_status"]
    assert d["max_trades"] == d["configured_max_trades"]                          # risk manager's own limit unchanged
    assert d["strategy_daily_cap"] == 3 and d["effective_max_trades"] == min(d["max_trades"], 3)
    from backend.copilot.full_context import _caps_extra
    assert _caps_extra(20) == {"strategy_daily_cap": 3, "effective_max_trades": 3}
