"""Comprehensive test suite for Phase 3: Upstox API v3 Semi-Automated Token Approval and Webhook Receiver.
Uses pure standard library + direct async router invocation for clean, zero-dependency testing.
"""
import os
import sys
import json
import types
import asyncio
import tempfile
import unittest
import importlib.util
from unittest.mock import patch, MagicMock
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import urllib.request
import urllib.error

# Ensure fastapi can be imported in test environment
if "fastapi" not in sys.modules:
    fastapi_mock = types.ModuleType("fastapi")
    class MockAPIRouter:
        def __init__(self, *args, **kwargs):
            self.routes = []
        def get(self, *args, **kwargs):
            return lambda f: f
        def post(self, *args, **kwargs):
            return lambda f: f
        def put(self, *args, **kwargs):
            return lambda f: f
        def delete(self, *args, **kwargs):
            return lambda f: f
        def all(self, *args, **kwargs):
            return lambda f: f

    class MockHTTPException(Exception):
        def __init__(self, status_code: int, detail: any = None):
            self.status_code = status_code
            self.detail = detail
            super().__init__(str(detail))

    fastapi_mock.APIRouter = MockAPIRouter
    fastapi_mock.HTTPException = MockHTTPException
    fastapi_mock.Request = MagicMock
    fastapi_mock.Response = MagicMock
    fastapi_mock.BackgroundTasks = MagicMock
    fastapi_mock.Query = lambda default=None, **kwargs: default
    fastapi_mock.Body = lambda default=None, **kwargs: default
    fastapi_mock.Path = lambda default=None, **kwargs: default
    fastapi_mock.Header = lambda default=None, **kwargs: default
    fastapi_mock.Depends = lambda dependency=None: dependency
    sys.modules["fastapi"] = fastapi_mock
    sys.modules["fastapi.responses"] = types.ModuleType("fastapi.responses")
    sys.modules["fastapi.responses"].JSONResponse = MagicMock
    sys.modules["fastapi.responses"].StreamingResponse = MagicMock
    sys.modules["fastapi.responses"].HTMLResponse = MagicMock
    sys.modules["fastapi.responses"].Response = MagicMock

from backend.database.db_manager import DatabaseManager
from backend.broker.auth import (
    UPSTOX_AUTH_URL,
    UPSTOX_TOKEN_URL,
    REQUEST_TOKEN_URL,
    request_token_approval,
)

# Load upstox_v3_auth directly without triggering all other API routers
_v3_spec = importlib.util.spec_from_file_location("upstox_v3_auth", "backend/api/routers/upstox_v3_auth.py")
upstox_v3_auth = importlib.util.module_from_spec(_v3_spec)
sys.modules["backend.api.routers.upstox_v3_auth"] = upstox_v3_auth
_v3_spec.loader.exec_module(upstox_v3_auth)

request_upstox_auth = upstox_v3_auth.request_upstox_auth
upstox_token_notifier_webhook = upstox_v3_auth.upstox_token_notifier_webhook
get_upstox_auth_status = upstox_v3_auth.get_upstox_auth_status
_auth_state = upstox_v3_auth._auth_state
UPSTOX_ERROR_CODE_MAP = upstox_v3_auth.UPSTOX_ERROR_CODE_MAP


class MockRequest:
    """Mock FastAPI/Starlette Request object."""
    def __init__(self, json_data=None, method="POST"):
        self._json_data = json_data
        self.method = method

    async def json(self):
        if self._json_data is None:
            raise ValueError("No JSON")
        return self._json_data


class MockBackgroundTasks:
    """Mock FastAPI BackgroundTasks object."""
    def __init__(self):
        self.tasks = []

    def add_task(self, func, *args, **kwargs):
        self.tasks.append((func, args, kwargs))


class TestUpstoxV3TokenApprovalSuite(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_v3.db")
        
        # Reset in-memory auth state
        _auth_state["status"] = "IDLE"
        _auth_state["requested_at"] = None
        _auth_state["authorization_expiry"] = 900
        _auth_state["approved_at"] = None
        _auth_state["last_error"] = None
        _auth_state["token_present"] = False

    def tearDown(self):
        self.temp_dir.cleanup()

    # --- TEST A: V3 Auth Request Success ---
    @patch.dict(os.environ, {"UPSTOX_CLIENT_ID": "test_cid_123", "UPSTOX_CLIENT_SECRET": "test_csec_456"})
    @patch("urllib.request.urlopen")
    def test_v3_auth_request_success(self, mock_urlopen):
        """Test A: POST /api/upstox/auth/request dispatches request to Upstox API v3 and returns safe response."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({
            "status": "success",
            "data": {
                "authorization_expiry": 900,
                "status": "pending_approval"
            }
        }).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        result = asyncio.run(request_upstox_auth())
        self.assertEqual(result.get("status"), "pending")
        self.assertIn("Approval request sent", result.get("message", ""))
        self.assertEqual(_auth_state["status"], "PENDING")

        # Verify client_secret was dispatched to Upstox
        req_obj = mock_urlopen.call_args[0][0]
        self.assertEqual(req_obj.full_url, "https://api.upstox.com/v3/login/auth/token/request/test_cid_123")
        sent_body = json.loads(req_obj.data.decode("utf-8"))
        self.assertEqual(sent_body.get("client_secret"), "test_csec_456")

        # Verify client_secret is NEVER in return value
        self.assertNotIn("test_csec_456", str(result))
        self.assertNotIn("client_secret", result)

    # --- TEST B: Missing Credentials ---
    @patch.dict(os.environ, {"UPSTOX_CLIENT_ID": "", "UPSTOX_CLIENT_SECRET": ""})
    def test_v3_auth_request_missing_credentials(self):
        """Test B: Missing client_id or client_secret raises HTTPException with 400 status."""
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(request_upstox_auth())
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(_auth_state["status"], "FAILED")

    # --- TEST C: Upstox Error Handling (UDAPI100069, UDAPI1123, UDAPI1124, UDAPI1155, UDAPI1157) ---
    @patch.dict(os.environ, {"UPSTOX_CLIENT_ID": "test_cid_123", "UPSTOX_CLIENT_SECRET": "test_csec_456"})
    @patch("urllib.request.urlopen")
    def test_v3_auth_request_upstox_errors(self, mock_urlopen):
        """Test C: Upstox API error codes mapped to user-safe messages without leaking secret."""
        from fastapi import HTTPException
        
        # 1. UDAPI100069 -> Already pending (returns status: pending)
        pending_json = json.dumps({
            "status": "error",
            "errors": [{"errorCode": "UDAPI100069", "message": "Already pending"}]
        }).encode("utf-8")
        mock_err = urllib.error.HTTPError(
            url="https://api.upstox.com",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=MagicMock(read=lambda b=pending_json: b)
        )
        mock_urlopen.side_effect = mock_err
        res = asyncio.run(request_upstox_auth())
        self.assertEqual(res.get("status"), "pending")
        self.assertEqual(res.get("message"), UPSTOX_ERROR_CODE_MAP["UDAPI100069"])

        # 2. Other error codes -> raise HTTPException(400)
        error_codes = ["UDAPI1123", "UDAPI1124", "UDAPI1155", "UDAPI1157"]
        for code in error_codes:
            err_json = json.dumps({
                "status": "error",
                "errors": [{"errorCode": code, "message": f"Raw {code} error"}]
            }).encode("utf-8")
            
            mock_err = urllib.error.HTTPError(
                url="https://api.upstox.com",
                code=400,
                msg="Bad Request",
                hdrs={},
                fp=MagicMock(read=lambda b=err_json: b)
            )
            mock_urlopen.side_effect = mock_err

            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(request_upstox_auth())
            
            self.assertEqual(ctx.exception.status_code, 400)
            detail = ctx.exception.detail
            self.assertEqual(detail.get("error_code"), code)
            self.assertEqual(detail.get("message"), UPSTOX_ERROR_CODE_MAP[code])
            self.assertNotIn("test_csec_456", str(detail))

    # --- TEST D: Webhook Receiver Success ---
    @patch.dict(os.environ, {"UPSTOX_CLIENT_ID": "test_cid_123"})
    def test_webhook_receiver_success(self):
        """Test D: Webhook saves token to DatabaseManager, settings, and updates status."""
        db = DatabaseManager(db_path=self.db_path)
        db.init_db()

        with patch.object(upstox_v3_auth, "_db", db):
            mock_req = MockRequest({
                "client_id": "test_cid_123",
                "message_type": "access_token",
                "access_token": "valid_upstox_pushed_token_xyz_9988",
                "user_id": "TEST_TRADER_1",
                "user_name": "Trader One",
                "timestamp": "2026-08-18T14:00:00Z"
            })

            result = asyncio.run(upstox_token_notifier_webhook(mock_req))
            self.assertEqual(result.get("status"), "success")
            self.assertIn("Token received", result.get("message", ""))

            # Verify SQLite persistence
            self.assertEqual(db.load_token(), "valid_upstox_pushed_token_xyz_9988")

            # Verify status diagnostic endpoint
            status = asyncio.run(get_upstox_auth_status())
            self.assertEqual(status.get("status"), "APPROVED")
            self.assertTrue(status.get("token_present"))

    # --- TEST E: Webhook Mismatched Client ID ---
    @patch.dict(os.environ, {"UPSTOX_CLIENT_ID": "test_cid_123"})
    def test_webhook_receiver_invalid_client_id(self):
        """Test E: Mismatched client_id in webhook payload returns 400 or 403 rejection."""
        from fastapi import HTTPException
        mock_req = MockRequest({
            "client_id": "mismatched_wrong_cid",
            "message_type": "access_token",
            "access_token": "some_token"
        })
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(upstox_token_notifier_webhook(mock_req))
        self.assertIn(ctx.exception.status_code, [400, 403])

    # --- TEST F: Webhook Missing Access Token ---
    @patch.dict(os.environ, {"UPSTOX_CLIENT_ID": "test_cid_123"})
    def test_webhook_receiver_missing_token(self):
        """Test F: Webhook missing access_token returns 400 rejection."""
        from fastapi import HTTPException
        mock_req = MockRequest({
            "client_id": "test_cid_123",
            "message_type": "access_token",
            "user_id": "TEST_TRADER_1"
        })
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(upstox_token_notifier_webhook(mock_req))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("missing", ctx.exception.detail.lower())

    # --- TEST G: Webhook Invalid Payload ---
    @patch.dict(os.environ, {"UPSTOX_CLIENT_ID": "test_cid_123"})
    def test_webhook_receiver_invalid_payload(self):
        """Test G: Invalid/empty payload raises 400."""
        from fastapi import HTTPException
        mock_req = MockRequest(None)
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(upstox_token_notifier_webhook(mock_req))
        self.assertEqual(ctx.exception.status_code, 400)

    # --- TEST H: Auth Status Lifecycle ---
    def test_v3_auth_status_lifecycle(self):
        """Test H: GET /api/upstox/auth/status reflects state transitions."""
        # 1. IDLE
        status = asyncio.run(get_upstox_auth_status())
        self.assertIn(status.get("status"), ["IDLE", "APPROVED"])
        self.assertIn("token_present", status)
        self.assertIn("authorization_expiry", status)

        # 2. PENDING
        _auth_state["status"] = "PENDING"
        _auth_state["requested_at"] = "2026-08-18T15:00:00Z"
        _auth_state["authorization_expiry"] = 900
        status = asyncio.run(get_upstox_auth_status())
        self.assertEqual(status.get("status"), "PENDING")
        self.assertEqual(status.get("requested_at"), "2026-08-18T15:00:00Z")

        # 3. FAILED
        _auth_state["status"] = "FAILED"
        _auth_state["last_error"] = "Invalid secret"
        status = asyncio.run(get_upstox_auth_status())
        self.assertEqual(status.get("status"), "FAILED")
        self.assertEqual(status.get("last_error"), "Invalid secret")

        # 4. EXPIRED
        _auth_state["status"] = "EXPIRED"
        _auth_state["last_error"] = "Request expired"
        status = asyncio.run(get_upstox_auth_status())
        self.assertEqual(status.get("status"), "EXPIRED")

    # --- TEST I: V2 Fallback Intact ---
    def test_v2_fallback_intact(self):
        """Test I: OAuth v2 URLs and fallback mechanisms remain present."""
        self.assertEqual(UPSTOX_AUTH_URL, "https://api.upstox.com/v2/login/authorization/dialog")
        self.assertEqual(UPSTOX_TOKEN_URL, "https://api.upstox.com/v2/login/authorization/token")
        self.assertEqual(REQUEST_TOKEN_URL, "https://api.upstox.com/v3/login/auth/token/request")

    # --- TEST J: Feature Flags in Version Endpoint ---
    def test_version_features(self):
        """Test J: /api/version reports upstox_v3_token_approval and upstox_notifier_webhook."""
        import ast
        with open("backend/api/main.py", "r") as f:
            content = f.read()
        self.assertIn('"upstox_v3_token_approval": True', content)
        self.assertIn('"upstox_notifier_webhook": True', content)


if __name__ == "__main__":
    unittest.main()
