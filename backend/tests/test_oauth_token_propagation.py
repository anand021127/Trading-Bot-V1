"""Regression tests for OAuth token propagation to TradingEngine and OrderManager.

Verifies that upon receiving a fresh OAuth access token:
1. TradingEngine.update_access_token() updates the underlying UpstoxClient.
2. OrderManager automatically sees the new token because it shares the same UpstoxClient instance.
3. The OAuth callback in backend.api.routers.settings propagates the fresh token to the shared engine.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Provide mock stubs for external libraries if running in a minimal environment without .venv packages
def _get_default_settings_dict():
    return {
        "mode": "paper",
        "broker": {
            "base_url": "https://api.upstox.com/v2",
            "websocket_url": "wss://api.upstox.com/v3/feed/market-data-feed",
        },
        "capital": {
            "total": 500000.0,
            "max_allocation_per_trade": 0.20,
            "cash_buffer": 0.40,
        },
        "risk": {
            "max_risk_per_trade_pct": 0.01,
            "max_daily_loss_pct": 0.02,
            "max_trades_per_day": 4,
            "max_concurrent_positions": 2,
            "max_consecutive_losses": 3,
            "pause_after_losses_minutes": 30,
            "max_position_exposure": 0.60,
            "max_option_tick_age_seconds": 30,
        },
        "paper_execution": {
            "slippage_pct": 0.05,
            "latency_ms": 0,
            "use_ltp_when_no_quote": True,
        },
        "strategy": {
            "name": "ORB_TREND_FOLLOWING",
            "timeframe_entry": "5minute",
            "timeframe_trend": "15minute",
            "orb_window_start": "09:15",
            "orb_window_end": "09:30",
            "entry_window_start": "09:30",
            "entry_window_end": "12:30",
            "exit_all_by": "14:45",
            "no_new_trades_after": "14:30",
        },
        "indicators": {
            "ema_fast": 20,
            "ema_slow": 50,
            "ema_trend": 200,
            "rsi_period": 14,
            "rsi_min": 55,
            "rsi_max": 75,
            "atr_period": 14,
            "choppiness_period": 14,
            "choppiness_max": 61.8,
            "volume_lookback": 20,
            "volume_multiplier": 1.5,
        },
        "filters": {
            "orb_min_width_atr_multiplier": 0.30,
            "orb_max_width_atr_multiplier": 2.50,
            "max_gap_up_pct": 0.02,
            "avoid_round_numbers_pct": 0.005,
            "min_body_pct_of_range": 0.60,
            "adx_min": 20.0,
        },
        "stop_loss": {"atr_multiplier": 1.5},
        "trailing_stop": {
            "stage2_trigger_r": 1.0,
            "stage3_trigger_r": 2.0,
            "stage3_atr_multiplier": 1.0,
            "stage4_trigger_r": 3.0,
            "stage4_atr_multiplier": 0.5,
        },
        "universe": {"option_indices": ["NIFTY50", "BANKNIFTY"]},
        "logging": {
            "level": "INFO",
            "log_dir": "logs",
            "max_file_size_mb": 10,
            "backup_count": 5,
        },
        "database": {"path": "data_cache/trading_bot.db"},
        "notifications": {
            "email_enabled": False,
            "telegram_enabled": False,
            "smtp_server": "smtp.gmail.com",
            "smtp_port": 587,
            "sender_email": "",
            "recipient_email": "",
            "rate_limit_seconds": 60,
        },
        "backtest": {
            "start_date": "2024-01-01",
            "end_date": "2024-06-30",
            "initial_capital": 500000.0,
            "commission_pct": 0.0003,
            "slippage_pct": 0.0005,
            "stt_pct": 0.0005,
        },
    }

if "yaml" not in sys.modules:
    m_yaml = MagicMock()
    m_yaml.safe_load = MagicMock(return_value=_get_default_settings_dict())
    m_yaml.dump = MagicMock(return_value="")
    sys.modules["yaml"] = m_yaml

if "pydantic" not in sys.modules:
    m_pydantic = MagicMock()
    class _BaseModel:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                if isinstance(v, dict):
                    setattr(self, k, _BaseModel(**v))
                else:
                    setattr(self, k, v)
        def __getattr__(self, name):
            return None
    m_pydantic.BaseModel = _BaseModel
    m_pydantic.Field = MagicMock(side_effect=lambda default=None, **kwargs: default)
    sys.modules["pydantic"] = m_pydantic

for mod_name in ["fastapi", "fastapi.responses", "fastapi.middleware", "fastapi.middleware.cors", "requests", "requests.adapters", "urllib3", "urllib3.util", "urllib3.util.retry", "httpx"]:
    if mod_name not in sys.modules:
        m = MagicMock()
        m.__path__ = []
        if mod_name == "fastapi":
            mock_router = MagicMock()
            mock_router.get = MagicMock(side_effect=lambda *a, **kw: (lambda fn: fn))
            mock_router.post = MagicMock(side_effect=lambda *a, **kw: (lambda fn: fn))
            mock_router.put = MagicMock(side_effect=lambda *a, **kw: (lambda fn: fn))
            mock_router.delete = MagicMock(side_effect=lambda *a, **kw: (lambda fn: fn))
            m.APIRouter = MagicMock(return_value=mock_router)
            m.HTTPException = Exception
            m.Query = MagicMock(side_effect=lambda default=None, **kwargs: default)
        elif mod_name == "fastapi.responses":
            m.StreamingResponse = MagicMock()
            m.JSONResponse = MagicMock()
            m.Response = MagicMock()
        sys.modules[mod_name] = m

from backend.api.routers.bot_control import get_engine, set_engine
from backend.api.routers.settings import _propagate_token_to_engine, token_callback
from backend.orders.order_manager import OrderManager
from backend.strategy.trading_engine import TradingEngine


class DummyUpstoxClient:
    """Mock UpstoxClient for unit testing without network or credentials."""
    def __init__(self, access_token: str = ""):
        self.access_token = access_token
        self.base_url = "https://api.upstox.com/v2"

    def get_positions_with_details(self):
        return []

    def get_nearest_expiry(self, symbol: str):
        return "2026-08-20"

    def get_option_chain(self, symbol: str, expiry: str):
        return []

    def get_historical_candles(self, symbol: str, interval: str, limit: int = 100):
        return []

    def get_multiple_quotes(self, symbols):
        return {s: {"ltp": 24000.0} for s in symbols}

    def get_quote_by_instrument_key(self, key: str):
        return {"ltp": 150.0}


class TestOAuthTokenPropagation(unittest.TestCase):
    """Test token propagation to TradingEngine and OrderManager."""

    def setUp(self):
        self.old_token = "mock-old-expired-token-123"
        self.new_token = "mock-fresh-access-token-456"

        self.mock_client = DummyUpstoxClient(access_token=self.old_token)
        self.mock_order_manager = OrderManager(
            client=self.mock_client,
            paper_mode=True,
        )

        with patch.object(TradingEngine, "hydrate_and_reconcile_positions", return_value={}):
            self.engine = TradingEngine(
                client=self.mock_client,
                order_manager=self.mock_order_manager,
            )

        set_engine(self.engine)

    def tearDown(self):
        set_engine(None)

    def test_shared_client_instance(self):
        """Verify TradingEngine and OrderManager share the exact same UpstoxClient instance."""
        self.assertIs(
            self.engine.client,
            self.engine.order_manager.client,
            "OrderManager must hold the exact same UpstoxClient object reference as TradingEngine",
        )
        self.assertEqual(self.engine.client.access_token, self.old_token)
        self.assertEqual(self.engine.order_manager.client.access_token, self.old_token)

    def test_update_access_token_success(self):
        """Verify update_access_token updates both TradingEngine.client and OrderManager.client."""
        self.engine.update_access_token(self.new_token)

        self.assertEqual(
            self.engine.client.access_token,
            self.new_token,
            "TradingEngine.client.access_token must update to new token",
        )
        self.assertEqual(
            self.engine.order_manager.client.access_token,
            self.new_token,
            "OrderManager.client.access_token must reflect the updated token",
        )

    def test_update_access_token_validation(self):
        """Verify empty tokens are rejected with ValueError."""
        with self.assertRaises(ValueError):
            self.engine.update_access_token("")

        with self.assertRaises(ValueError):
            self.engine.update_access_token(None)  # type: ignore

        # Token should remain unchanged after failed validation
        self.assertEqual(self.engine.client.access_token, self.old_token)

    def test_propagate_token_to_engine_helper(self):
        """Verify _propagate_token_to_engine finds registered engine and updates its token."""
        self.assertEqual(get_engine(), self.engine)
        _propagate_token_to_engine(self.new_token)

        self.assertEqual(self.engine.client.access_token, self.new_token)
        self.assertEqual(self.engine.order_manager.client.access_token, self.new_token)

    def test_propagate_token_when_engine_uninitialized(self):
        """Verify _propagate_token_to_engine executes safely when no engine is registered."""
        set_engine(None)
        # Should not raise any exception
        _propagate_token_to_engine("some-token")

    def test_token_callback_get_end_to_end_propagation(self):
        """Verify token_callback_get exchanges auth code and updates engine client token."""
        fake_response = MagicMock()
        fake_response.json.return_value = {
            "access_token": self.new_token,
            "token_type": "Bearer",
        }

        mock_http_client = AsyncMock()
        mock_http_client.post.return_value = fake_response
        mock_http_client.__aenter__.return_value = mock_http_client
        mock_http_client.__aexit__.return_value = None

        with patch("httpx.AsyncClient", return_value=mock_http_client), \
             patch("backend.broker.upstox_client.UpstoxClient.is_token_valid", return_value=True), \
             patch("backend.api.routers.settings._restart_websocket_client") as mock_ws_restart, \
             patch("backend.api.routers.settings._db.save_token") as mock_save_token:

            resp = asyncio.run(token_callback("sample_auth_code"))

            self.assertEqual(os.environ.get("UPSTOX_ACCESS_TOKEN"), self.new_token)
            self.assertEqual(
                self.engine.client.access_token,
                self.new_token,
                "TradingEngine.client.access_token must be updated by OAuth callback",
            )
            self.assertEqual(
                self.engine.order_manager.client.access_token,
                self.new_token,
                "OrderManager.client.access_token must be updated by OAuth callback",
            )
            mock_ws_restart.assert_called_once_with(self.new_token)
            mock_save_token.assert_called_once_with(self.new_token)


if __name__ == "__main__":
    unittest.main()
