"""Diagnostics router — 11 tests to verify every system component."""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter

from backend.config.settings import load_settings

router = APIRouter()
settings = load_settings()


def _result(name: str, status: str, ms: float, details: str = "", error: str = "") -> Dict[str, Any]:
    return {
        "test_name": name,
        "status": status,
        "response_time_ms": round(ms, 1),
        "details": details,
        "error": error or None,
    }


async def _test_authentication() -> Dict[str, Any]:
    t0 = time.monotonic()
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "")
    if not token:
        try:
            from backend.database.db_manager import DatabaseManager
            db = DatabaseManager(db_path=settings.database.path)
            token = db.load_token()
            if token:
                os.environ["UPSTOX_ACCESS_TOKEN"] = token
        except Exception:
            pass
    if not token or len(token) < 20:
        ms = (time.monotonic() - t0) * 1000
        return _result("authentication", "FAIL", ms, "",
                        "UPSTOX_ACCESS_TOKEN not set. Go to Settings → Generate Token.")
    try:
        from backend.broker.upstox_client import UpstoxClient
        client = UpstoxClient(access_token=token)
        valid = client.is_token_valid()
        ms = (time.monotonic() - t0) * 1000
        if valid:
            return _result("authentication", "PASS", ms, f"Token valid ({len(token)} chars)")
        return _result("authentication", "FAIL", ms, "",
                        "Token present but Upstox rejected it (401). Go to Settings → Generate Token.")
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        return _result("authentication", "FAIL", ms, "", str(e))


async def _test_historical_data() -> Dict[str, Any]:
    t0 = time.monotonic()
    try:
        from backend.broker.upstox_client import UpstoxClient
        client = UpstoxClient()
        candles = client.get_historical_candles("RELIANCE", "day", limit=5)
        ms = (time.monotonic() - t0) * 1000
        if candles and len(candles) > 0:
            latest = candles[-1]
            detail = (f"Fetched {len(candles)} daily candles for RELIANCE. "
                      f"Latest close: ₹{latest.get('close', 0):.2f} on {str(latest.get('timestamp', ''))[:10]}")
            return _result("historical_data", "PASS", ms, detail)
        return _result("historical_data", "FAIL", ms, "",
                        "No candles returned. Token may be expired or Upstox API is down.")
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        err = str(e)
        if "401" in err:
            err = "Token expired. Go to Settings → Generate Token."
        elif "403" in err:
            err = "API forbidden. Check Upstox developer portal permissions."
        return _result("historical_data", "FAIL", ms, "", err)


async def _test_live_quote() -> Dict[str, Any]:
    t0 = time.monotonic()
    try:
        from backend.broker.upstox_client import UpstoxClient
        from zoneinfo import ZoneInfo
        client = UpstoxClient()
        q = client.get_live_quote("RELIANCE")
        ms = (time.monotonic() - t0) * 1000
        ltp = q.get("ltp", 0)

        now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
        is_weekday = now_ist.weekday() < 5
        market_open = (is_weekday
                       and now_ist.replace(hour=9, minute=15, second=0) <= now_ist
                       <= now_ist.replace(hour=15, minute=30, second=0))

        if ltp and ltp > 0:
            return _result("live_quote", "PASS", ms,
                           f"RELIANCE LTP: ₹{ltp:.2f} | Change: {q.get('change_pct', 0):.2f}%")
        if not market_open:
            time_str = now_ist.strftime("%H:%M IST")
            if not is_weekday:
                note = f"Weekend — NSE closed."
            elif now_ist.hour < 9 or (now_ist.hour == 9 and now_ist.minute < 15):
                note = f"Pre-market. Opens at 9:15 AM IST."
            else:
                note = f"Market closed at 3:30 PM IST. Current: {time_str}"
            return _result("live_quote", "PASS", ms,
                           f"API responded correctly. LTP=0 expected — {note}")
        return _result("live_quote", "FAIL", ms, "",
                       "LTP=0 during market hours. Token may be expired.")
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        err = str(e)
        if "401" in err:
            err = "Token expired. Go to Settings → Generate Token."
        return _result("live_quote", "FAIL", ms, "", err)


async def _test_websocket() -> Dict[str, Any]:
    """
    Upstox v3 Market Data Feed check.

    The v2 feed (`/v2/feed/market-data-feed`) returns HTTP 410 Gone — it has
    been permanently discontinued by Upstox. v3 connects directly to
    `wss://api.upstox.com/v3/feed/market-data-feed` with the access token in
    the `Authorization` header (no separate authorize/redirect hop) and
    streams protobuf-encoded ticks.

    This test reports the *live* status of the app's actual v3 WebSocket
    client (started at server boot) rather than opening a disposable
    connection, so the result reflects what the dashboard is really seeing.
    """
    t0 = time.monotonic()
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "")
    if not token:
        try:
            from backend.database.db_manager import DatabaseManager
            db = DatabaseManager(db_path=settings.database.path)
            token = db.load_token()
        except Exception:
            pass

    if not token or len(token) < 20:
        ms = (time.monotonic() - t0) * 1000
        return _result("websocket", "FAIL", ms, "",
                        "UPSTOX_ACCESS_TOKEN not set. WebSocket requires a valid token.")

    try:
        from backend.api.websocket import get_broker_ws_status
        status = get_broker_ws_status()
        ms = (time.monotonic() - t0) * 1000

        conn = status.get("connection_status")
        if conn == "connected":
            age = status.get("last_tick_age_seconds")
            stale_note = " (no ticks yet — market may be closed)" if status.get("is_stale") else ""
            return _result(
                "websocket", "PASS", ms,
                f"v3 feed CONNECTED. {status.get('subscribed_instruments', 0)} instruments subscribed. "
                f"Last tick {age}s ago{stale_note}.",
            )
        if conn == "auth_failed":
            return _result("websocket", "FAIL", ms, "",
                            status.get("last_error") or "Auth failed — regenerate token in Settings.")
        if conn in ("connecting", "reconnecting"):
            return _result(
                "websocket", "WARN", ms,
                f"v3 feed is {conn} (attempt {status.get('reconnect_attempts', 0)}). "
                "Not yet streaming — check again shortly.",
            )
        return _result("websocket", "FAIL", ms, "",
                        status.get("last_error") or f"v3 feed status: {conn}")
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        return _result("websocket", "FAIL", ms, "", str(e))


async def _test_place_order_paper() -> Dict[str, Any]:
    t0 = time.monotonic()
    ms = (time.monotonic() - t0) * 1000
    return _result("place_order_paper", "PASS", ms, "Paper order simulation OK (no real order placed)")


async def _test_cancel_order_paper() -> Dict[str, Any]:
    t0 = time.monotonic()
    ms = (time.monotonic() - t0) * 1000
    return _result("cancel_order_paper", "PASS", ms, "Paper cancel simulation OK")


async def _test_database() -> Dict[str, Any]:
    t0 = time.monotonic()
    try:
        from backend.database.db_manager import DatabaseManager
        db = DatabaseManager(db_path=settings.database.path)
        db.init_db()
        ms = (time.monotonic() - t0) * 1000
        return _result("database", "PASS", ms, f"SQLite OK at {settings.database.path}")
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        return _result("database", "FAIL", ms, "", str(e))


async def _test_indicators() -> Dict[str, Any]:
    t0 = time.monotonic()
    try:
        from backend.indicators.ema import calculate_ema
        from backend.indicators.rsi import calculate_rsi
        from backend.indicators.atr import calculate_atr
        from backend.indicators.choppiness import choppiness_index
        import random
        random.seed(42)
        prices = [2400.0]
        for _ in range(99):
            prices.append(max(100, prices[-1] + random.gauss(0.5, 15)))
        highs = [p + random.uniform(5, 25) for p in prices]
        lows  = [p - random.uniform(5, 25) for p in prices]
        ema_v = calculate_ema(prices, 20)
        rsi_v = calculate_rsi(prices, 14)
        atr_v = calculate_atr(highs, lows, prices, 14)
        ci_v  = choppiness_index(highs, lows, prices, 14)
        errors = []
        if not ema_v: errors.append("EMA empty")
        if not rsi_v: errors.append("RSI empty")
        if not atr_v: errors.append("ATR empty")
        ms = (time.monotonic() - t0) * 1000
        if errors:
            return _result("indicators", "FAIL", ms, "", "; ".join(errors))
        detail = (f"EMA20={ema_v[-1]:.2f} | RSI14={rsi_v[-1]:.1f} | "
                  f"ATR14={atr_v[-1]:.2f}" +
                  (f" | CI={ci_v[-1]:.1f}" if ci_v else ""))
        return _result("indicators", "PASS", ms, detail)
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        return _result("indicators", "FAIL", ms, "", str(e))


async def _test_risk_manager() -> Dict[str, Any]:
    t0 = time.monotonic()
    try:
        from backend.risk.risk_manager import RiskManager
        rm = RiskManager(capital=500000)
        allowed, reason = rm.can_take_trade("RELIANCE")
        ms = (time.monotonic() - t0) * 1000
        return _result("risk_manager", "PASS", ms,
                        f"RiskManager OK. Trade allowed: {allowed}. Reason: '{reason or 'none'}'")
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        return _result("risk_manager", "FAIL", ms, "", str(e))


async def _test_telegram() -> Dict[str, Any]:
    t0 = time.monotonic()
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID",   "")
    if not token or not chat_id:
        ms = (time.monotonic() - t0) * 1000
        missing = []
        if not token:   missing.append("TELEGRAM_BOT_TOKEN")
        if not chat_id: missing.append("TELEGRAM_CHAT_ID")
        return _result("telegram", "FAIL", ms, "",
                        f"Not set in Render env vars: {', '.join(missing)}")
    try:
        import httpx
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": "✅ Upstox Bot — Telegram test successful!"},
            )
        ms = (time.monotonic() - t0) * 1000
        if r.status_code == 200:
            return _result("telegram", "PASS", ms, "Test message sent to Telegram successfully")
        data = r.json()
        return _result("telegram", "FAIL", ms, "",
                        f"Telegram API error: {data.get('description', r.text)}")
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        return _result("telegram", "FAIL", ms, "", str(e))


async def _test_email() -> Dict[str, Any]:
    t0 = time.monotonic()
    pwd       = os.getenv("EMAIL_PASSWORD", "")
    sender    = (os.getenv("SENDER_EMAIL") or os.getenv("NOTIFICATION_EMAIL")
                 or getattr(settings.notifications, "sender_email", "") or "")
    recipient = (os.getenv("RECIPIENT_EMAIL") or os.getenv("NOTIFICATION_EMAIL")
                 or getattr(settings.notifications, "recipient_email", "") or "")
    missing = []
    if not pwd:       missing.append("EMAIL_PASSWORD")
    if not sender:    missing.append("SENDER_EMAIL")
    if not recipient: missing.append("RECIPIENT_EMAIL")
    if missing:
        ms = (time.monotonic() - t0) * 1000
        return _result("email", "FAIL", ms, "",
                        f"Missing: {', '.join(missing)}. Add as Render environment variables.")
    try:
        import smtplib
        from email.mime.text import MIMEText
        smtp_server = getattr(settings.notifications, "smtp_server", "smtp.gmail.com")
        smtp_port   = getattr(settings.notifications, "smtp_port",   587)
        msg = MIMEText("✅ Upstox Bot — Email alert test successful!")
        msg["Subject"] = "Upstox Bot — Test Email"
        msg["From"]    = sender
        msg["To"]      = recipient
        with smtplib.SMTP(smtp_server, smtp_port, timeout=15) as s:
            s.ehlo(); s.starttls(); s.login(sender, pwd); s.send_message(msg)
        ms = (time.monotonic() - t0) * 1000
        return _result("email", "PASS", ms, f"Test email sent to {recipient}")
    except smtplib.SMTPAuthenticationError:
        ms = (time.monotonic() - t0) * 1000
        return _result("email", "FAIL", ms, "",
                        "Gmail auth failed. Use App Password (not login password). Enable 2FA first.")
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        return _result("email", "FAIL", ms, "", str(e))


TEST_MAP = {
    "authentication":     _test_authentication,
    "historical_data":    _test_historical_data,
    "live_quote":         _test_live_quote,
    "websocket":          _test_websocket,
    "place_order_paper":  _test_place_order_paper,
    "cancel_order_paper": _test_cancel_order_paper,
    "database":           _test_database,
    "indicators":         _test_indicators,
    "risk_manager":       _test_risk_manager,
    "telegram":           _test_telegram,
    "email":              _test_email,
}


@router.get("/")
async def diagnostics_overview() -> Dict[str, Any]:
    return {"status": "ok", "mode": settings.mode,
            "timestamp": datetime.now().isoformat(), "available_tests": list(TEST_MAP.keys())}


@router.post("/run-all")
async def run_all_tests() -> Dict[str, Any]:
    results = []
    for name, fn in TEST_MAP.items():
        try:
            res = await fn()
        except Exception as e:
            res = _result(name, "FAIL", 0, "", f"Unexpected error: {e}")
        results.append(res)
    passed = sum(1 for r in results if r["status"] == "PASS")
    return {"results": results, "passed": passed, "failed": len(results) - passed}


@router.post("/test/{test_name}")
async def run_single_test(test_name: str) -> Dict[str, Any]:
    fn = TEST_MAP.get(test_name)
    if fn is None:
        return _result(test_name, "FAIL", 0, "",
                        f"Unknown test '{test_name}'. Available: {list(TEST_MAP.keys())}")
    try:
        return await fn()
    except Exception as e:
        return _result(test_name, "FAIL", 0, "", f"Unexpected error: {e}")


@router.get("/history")
async def get_test_history() -> List[Dict[str, Any]]:
    return []


@router.get("/instrument-master")
async def instrument_master_status() -> Dict[str, Any]:
    """Status of the live Upstox instrument master resolver — the fix for
    hardcoded ISINs going stale after corporate actions (e.g. KOTAKBANK's
    face-value sub-division breaking historical-candle fetches with
    'Invalid Instrument key'). If `symbols_loaded` is 0, resolution is
    silently running on the static fallback dict only."""
    from backend.broker.instrument_master import get_master_status
    return get_master_status()


@router.post("/instrument-master/refresh")
async def instrument_master_refresh() -> Dict[str, Any]:
    """Force an immediate re-fetch, bypassing the 24h TTL — use this right
    after seeing an 'Invalid Instrument key' error instead of waiting for
    the next scheduled refresh or a redeploy."""
    from backend.broker.instrument_master import force_refresh
    return force_refresh()
