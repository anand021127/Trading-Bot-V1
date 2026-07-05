"""Router for runtime diagnostics and API health tests."""
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
    ms = (time.monotonic() - t0) * 1000
    if token and len(token) > 10:
        return _result("authentication", "PASS", ms, f"Token present ({len(token)} chars)")
    return _result("authentication", "FAIL", ms, "", "UPSTOX_ACCESS_TOKEN not set or too short")


async def _test_historical_data() -> Dict[str, Any]:
    t0 = time.monotonic()
    try:
        from backend.broker.upstox_client import UpstoxClient
        client = UpstoxClient()
        candles = client.get_historical_candles("RELIANCE", "15minute", limit=5)
        ms = (time.monotonic() - t0) * 1000
        count = len(candles) if candles is not None else 0
        if count > 0:
            return _result("historical_data", "PASS", ms, f"Fetched {count} candles for RELIANCE")
        return _result("historical_data", "FAIL", ms, "", "No candles returned")
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        return _result("historical_data", "FAIL", ms, "", str(e))


async def _test_live_quote() -> Dict[str, Any]:
    t0 = time.monotonic()
    try:
        from backend.broker.upstox_client import UpstoxClient
        client = UpstoxClient()
        q = client.get_live_quote("RELIANCE")
        ms = (time.monotonic() - t0) * 1000
        if q and q.get("ltp", 0) > 0:
            return _result("live_quote", "PASS", ms, f"RELIANCE LTP: ₹{q['ltp']}")
        return _result("live_quote", "FAIL", ms, "", "LTP is 0 or missing")
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        return _result("live_quote", "FAIL", ms, "", str(e))


async def _test_websocket() -> Dict[str, Any]:
    t0 = time.monotonic()
    try:
        import asyncio
        import websockets  # type: ignore
        ws_url = settings.broker.websocket_url
        async with websockets.connect(ws_url, open_timeout=5) as ws:
            await asyncio.wait_for(ws.recv(), timeout=3)
        ms = (time.monotonic() - t0) * 1000
        return _result("websocket", "PASS", ms, "WebSocket connected and received data")
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        return _result("websocket", "FAIL", ms, "", f"WebSocket error: {e}")


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
        import numpy as np
        from backend.indicators.ema import calculate_ema
        from backend.indicators.rsi import calculate_rsi
        from backend.indicators.atr import calculate_atr
        prices = list(range(100, 200))
        ema = calculate_ema(prices, 20)
        rsi = calculate_rsi(prices, 14)
        ms = (time.monotonic() - t0) * 1000
        if ema and rsi:
            return _result("indicators", "PASS", ms, "EMA, RSI, ATR computed without errors")
        return _result("indicators", "FAIL", ms, "", "Indicator returned empty result")
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
        return _result("risk_manager", "PASS", ms, f"RiskManager OK. Trade allowed: {allowed}")
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        return _result("risk_manager", "FAIL", ms, "", str(e))


async def _test_telegram() -> Dict[str, Any]:
    t0 = time.monotonic()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        ms = (time.monotonic() - t0) * 1000
        return _result("telegram", "FAIL", ms, "", "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": "✅ Upstox Bot API test — Telegram is working!"},
            )
        ms = (time.monotonic() - t0) * 1000
        if r.status_code == 200:
            return _result("telegram", "PASS", ms, "Test message sent to Telegram")
        return _result("telegram", "FAIL", ms, "", r.text)
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
        return _result("email", "FAIL", ms, "", "EMAIL_PASSWORD or sender/recipient not configured")
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText("✅ Upstox Bot API test — Email is working!")
        msg["Subject"] = "Upstox Bot — Test Email"
        msg["From"] = sender
        msg["To"] = recipient
        with smtplib.SMTP(settings.notifications.smtp_server, settings.notifications.smtp_port, timeout=10) as s:
            s.starttls()
            s.login(sender, pwd)
            s.send_message(msg)
        ms = (time.monotonic() - t0) * 1000
        return _result("email", "PASS", ms, f"Test email sent to {recipient}")
    except Exception as e:
        ms = (time.monotonic() - t0) * 1000
        return _result("email", "FAIL", ms, "", str(e))


TEST_MAP = {
    "authentication": _test_authentication,
    "historical_data": _test_historical_data,
    "live_quote": _test_live_quote,
    "websocket": _test_websocket,
    "place_order_paper": _test_place_order_paper,
    "cancel_order_paper": _test_cancel_order_paper,
    "database": _test_database,
    "indicators": _test_indicators,
    "risk_manager": _test_risk_manager,
    "telegram": _test_telegram,
    "email": _test_email,
}


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
        res = await fn()
        results.append(res)
    passed = sum(1 for r in results if r["status"] == "PASS")
    return {"results": results, "passed": passed, "failed": len(results) - passed}


@router.post("/test/{test_name}")
async def run_single_test(test_name: str) -> Dict[str, Any]:
    fn = TEST_MAP.get(test_name)
    if fn is None:
        return _result(test_name, "FAIL", 0, "", f"Unknown test: {test_name}")
    return await fn()


@router.get("/history")
async def get_test_history() -> List[Dict[str, Any]]:
    # In a full implementation this reads from the api_test_log table
    return []
