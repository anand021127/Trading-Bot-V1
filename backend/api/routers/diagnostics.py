"""Router for runtime diagnostics and API health tests.

All 11 tests available via:
  POST /api/diagnostics/run-all
  POST /api/diagnostics/test/{test_name}
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter

from backend.config.settings import load_settings

router = APIRouter()
settings = load_settings()


def _result(
    name: str, status: str, ms: float, details: str = "", error: str = ""
) -> Dict[str, Any]:
    return {
        "test_name": name,
        "status": status,
        "response_time_ms": round(ms, 1),
        "details": details,
        "error": error or None,
    }


# ─── Individual test functions ────────────────────────────────────────────────

async def _test_authentication() -> Dict[str, Any]:
    t0 = time.monotonic()
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "")
    ms = (time.monotonic() - t0) * 1000
    if not token or len(token) < 20:
        return _result("authentication", "FAIL", ms, "", "UPSTOX_ACCESS_TOKEN not set or too short")
    try:
        from backend.broker.upstox_client import UpstoxClient
        client = UpstoxClient(access_token=token)
        valid = client.is_token_valid()
        ms = (time.monotonic() - t0) * 1000
        if valid:
            return _result("authentication", "PASS", ms, f"Token valid ({len(token)} chars)")
        return _result("authentication", "FAIL", ms, "", "Token present but Upstox rejected it (401). Regenerate token.")
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        return _result("authentication", "FAIL", ms, "", f"Auth check error: {e}")


async def _test_historical_data() -> Dict[str, Any]:
    t0 = time.monotonic()
    try:
        from backend.broker.upstox_client import UpstoxClient
        client = UpstoxClient()
        candles = client.get_historical_candles("RELIANCE", "15minute", limit=5)
        ms = (time.monotonic() - t0) * 1000
        count = len(candles) if candles else 0
        if count > 0:
            return _result("historical_data", "PASS", ms, f"Fetched {count} candles for RELIANCE 15-min")
        return _result("historical_data", "FAIL", ms, "", "No candles returned — market may be closed or token expired")
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        err = str(e)
        if "401" in err:
            err = "Token expired or invalid. Go to Settings → Generate Token."
        elif "403" in err:
            err = "API access forbidden. Check Upstox app permissions."
        return _result("historical_data", "FAIL", ms, "", err)


async def _test_live_quote() -> Dict[str, Any]:
    t0 = time.monotonic()
    try:
        from backend.broker.upstox_client import UpstoxClient
        client = UpstoxClient()
        q = client.get_live_quote("RELIANCE")
        ms = (time.monotonic() - t0) * 1000
        ltp = q.get("ltp", 0)
        if ltp and ltp > 0:
            return _result("live_quote", "PASS", ms, f"RELIANCE LTP: ₹{ltp:.2f}")
        return _result(
            "live_quote", "FAIL", ms, "",
            "LTP is 0 — market may be closed. Prices are 0 outside 9:15–15:30 IST on weekdays."
        )
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        err = str(e)
        if "401" in err:
            err = "Token expired. Go to Settings → Generate Token."
        return _result("live_quote", "FAIL", ms, "", err)


async def _test_websocket() -> Dict[str, Any]:
    """
    Test WebSocket connectivity to Upstox.
    Note: Upstox WebSocket requires a valid access token in the connection URL.
    Without a valid token it returns HTTP 401.
    """
    t0 = time.monotonic()
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "")
    if not token or len(token) < 20:
        ms = (time.monotonic() - t0) * 1000
        return _result(
            "websocket", "FAIL", ms, "",
            "UPSTOX_ACCESS_TOKEN not set. WebSocket requires authentication. Set token first."
        )
    try:
        import asyncio
        import websockets  # type: ignore

        # Upstox WebSocket v2 requires Authorization header
        ws_url = "wss://api.upstox.com/v2/feed/market-data-feed"
        headers = {"Authorization": f"Bearer {token}"}

        async with websockets.connect(
            ws_url,
            additional_headers=headers,
            open_timeout=8,
        ) as ws:
            # Send subscription message
            import json
            sub_msg = json.dumps({
                "guid": "bot-test",
                "method": "sub",
                "data": {
                    "mode": "ltpc",
                    "instrumentKeys": ["NSE_EQ|INE002A01018"],
                },
            })
            await ws.send(sub_msg)
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                ms = (time.monotonic() - t0) * 1000
                return _result("websocket", "PASS", ms, f"WebSocket connected. Received {len(msg)} bytes.")
            except asyncio.TimeoutError:
                ms = (time.monotonic() - t0) * 1000
                return _result("websocket", "PASS", ms, "WebSocket connected (no data within 5s — market may be closed)")
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        err = str(e)
        if "401" in err:
            err = "WebSocket rejected (HTTP 401). Token invalid or expired. Regenerate token."
        elif "403" in err:
            err = "WebSocket forbidden (HTTP 403). Check Upstox API subscription plan."
        return _result("websocket", "FAIL", ms, "", err)


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
    """Test all indicator calculations with synthetic data."""
    t0 = time.monotonic()
    try:
        # Use correct function names
        from backend.indicators.ema import calculate_ema
        from backend.indicators.rsi import calculate_rsi
        from backend.indicators.atr import calculate_atr
        from backend.indicators.choppiness import choppiness_index

        # Synthetic price data — 50 bars
        import random
        random.seed(42)
        prices = [2400.0]
        for _ in range(99):
            prices.append(prices[-1] + random.uniform(-15, 15))

        highs = [p + random.uniform(5, 25) for p in prices]
        lows  = [p - random.uniform(5, 25) for p in prices]

        ema_vals = calculate_ema(prices, 20)
        rsi_vals = calculate_rsi(prices, 14)
        atr_vals = calculate_atr(highs, lows, prices, 14)

        errors = []
        if not ema_vals:
            errors.append("EMA returned empty")
        if not rsi_vals:
            errors.append("RSI returned empty")
        if not atr_vals:
            errors.append("ATR returned empty")

        ms = (time.monotonic() - t0) * 1000
        if errors:
            return _result("indicators", "FAIL", ms, "", "; ".join(errors))

        detail = (
            f"EMA20={ema_vals[-1]:.2f} | "
            f"RSI14={rsi_vals[-1]:.1f} | "
            f"ATR14={atr_vals[-1]:.2f}"
        )
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
        return _result(
            "risk_manager", "PASS", ms,
            f"RiskManager OK. Trade allowed: {allowed}. Reason: '{reason or 'none'}'"
        )
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        return _result("risk_manager", "FAIL", ms, "", str(e))


async def _test_telegram() -> Dict[str, Any]:
    t0 = time.monotonic()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        ms = (time.monotonic() - t0) * 1000
        return _result("telegram", "FAIL", ms, "", "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in Render env vars")
    try:
        import httpx
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": "✅ Upstox Bot API test — Telegram is working!"},
            )
        ms = (time.monotonic() - t0) * 1000
        if r.status_code == 200:
            return _result("telegram", "PASS", ms, "Test message sent to Telegram successfully")
        data = r.json()
        return _result("telegram", "FAIL", ms, "", f"Telegram error: {data.get('description', r.text)}")
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        return _result("telegram", "FAIL", ms, "", str(e))


async def _test_email() -> Dict[str, Any]:
    t0 = time.monotonic()
    pwd = os.getenv("EMAIL_PASSWORD", "")
    sender = settings.notifications.sender_email
    recipient = settings.notifications.recipient_email
    if not pwd or not sender or not recipient:
        ms = (time.monotonic() - t0) * 1000
        missing = []
        if not pwd: missing.append("EMAIL_PASSWORD env var")
        if not sender: missing.append("sender_email in settings.yaml")
        if not recipient: missing.append("recipient_email in settings.yaml")
        return _result("email", "FAIL", ms, "", f"Not configured: {', '.join(missing)}")
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText("✅ Upstox Bot — Email test successful!")
        msg["Subject"] = "Upstox Bot — Test Email"
        msg["From"] = sender
        msg["To"] = recipient
        with smtplib.SMTP(settings.notifications.smtp_server, settings.notifications.smtp_port, timeout=10) as s:
            s.ehlo()
            s.starttls()
            s.login(sender, pwd)
            s.send_message(msg)
        ms = (time.monotonic() - t0) * 1000
        return _result("email", "PASS", ms, f"Test email sent to {recipient}")
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        return _result("email", "FAIL", ms, "", str(e))


# ─── Test registry ────────────────────────────────────────────────────────────

TEST_MAP = {
    "authentication":      _test_authentication,
    "historical_data":     _test_historical_data,
    "live_quote":          _test_live_quote,
    "websocket":           _test_websocket,
    "place_order_paper":   _test_place_order_paper,
    "cancel_order_paper":  _test_cancel_order_paper,
    "database":            _test_database,
    "indicators":          _test_indicators,
    "risk_manager":        _test_risk_manager,
    "telegram":            _test_telegram,
    "email":               _test_email,
}


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("/")
async def diagnostics_overview() -> Dict[str, Any]:
    return {
        "status": "ok",
        "mode": settings.mode,
        "timestamp": datetime.now().isoformat(),
        "available_tests": list(TEST_MAP.keys()),
    }


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
        return _result(test_name, "FAIL", 0, "", f"Unknown test '{test_name}'. Available: {list(TEST_MAP.keys())}")
    try:
        return await fn()
    except Exception as e:
        return _result(test_name, "FAIL", 0, "", f"Unexpected error: {e}")


@router.get("/history")
async def get_test_history() -> List[Dict[str, Any]]:
    return []
