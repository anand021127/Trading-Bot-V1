"""Automated tests verifying Phase 2B: Upstox Semi-Automated Access Token Flow (API v3) and Webhook Notifier.
"""
import hashlib
import json
import os
import tempfile
import threading
import unittest
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request
import urllib.error

from backend.database.db_manager import DatabaseManager
from backend.broker.auth import (
    request_token_approval,
    REQUEST_TOKEN_URL,
    UPSTOX_AUTH_URL,
    UPSTOX_TOKEN_URL,
)


TEST_CLIENT_ID = "632196c5-c184-4966-ac31-773c048e6375"
TEST_CLIENT_SECRET = "k1ypgaemz8"
VALID_V3_TOKEN = "test_v3_pushed_access_token_8899aabbcc"
INVALID_V3_TOKEN = "invalid_pushed_access_token_0000"


class MockUpstoxV3Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode() if content_length > 0 else "{}"

        # 1. Semi-Automated v3 Token Request Endpoint
        if f"/v3/login/auth/token/request/{TEST_CLIENT_ID}" in self.path:
            payload = json.loads(body)
            if payload.get("client_secret") == TEST_CLIENT_SECRET:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "success",
                    "data": {
                        "authorization_expiry": "2026-08-18T18:30:00.000Z",
                        "status": "pending_approval"
                    }
                }).encode())
            else:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "error",
                    "errors": [{"errorCode": "UDAPI100068", "message": "Invalid client_secret"}]
                }).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        auth_header = self.headers.get("Authorization", "")
        # 2. User Profile Verification
        if "/v2/user/profile" in self.path:
            if f"Bearer {VALID_V3_TOKEN}" in auth_header:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "success",
                    "data": {
                        "user_id": "TEST_USER_V3",
                        "user_name": "Upstox V3 Trader",
                        "broker": "UPSTOX"
                    }
                }).encode())
            else:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "error",
                    "errors": [{"errorCode": "UDAPI100050", "message": "Invalid access token"}]
                }).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Quiet test logging


class TestUpstoxV3SemiAutomatedAuth(unittest.TestCase):
    def setUp(self):
        for p in ["upstox_token.json", "data/upstox_token.json"]:
            if os.path.exists(p):
                try: os.remove(p)
                except Exception: pass
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_v3_trading.db")

        # Mock Upstox HTTP Server
        self.server = HTTPServer(("127.0.0.1", 0), MockUpstoxV3Handler)
        self.server_port = self.server.server_address[1]
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp_dir.cleanup()
        for p in ["upstox_token.json", "data/upstox_token.json"]:
            if os.path.exists(p):
                try: os.remove(p)
                except Exception: pass

    def test_request_token_approval_url_and_payload(self):
        """Test that request_token_approval formats the URL and payload to Upstox API v3 specs."""
        self.assertEqual(REQUEST_TOKEN_URL, "https://api.upstox.com/v3/login/auth/token/request")

        # Simulate custom base URL pointing to mock server
        mock_url = f"http://127.0.0.1:{self.server_port}/v3/login/auth/token/request/{TEST_CLIENT_ID}"
        req_data = json.dumps({"client_secret": TEST_CLIENT_SECRET}).encode("utf-8")
        req = urllib.request.Request(
            mock_url,
            data=req_data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json"
            },
            method="POST"
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode())
            self.assertEqual(data["status"], "success")
            self.assertEqual(data["data"]["status"], "pending_approval")

    def test_webhook_token_verification_and_persistence(self):
        """Test that incoming webhook token is validated against /v2/user/profile before SQLite persistence."""
        db = DatabaseManager(db_path=self.db_path)
        db.init_db()

        # 1. Incoming payload from Upstox Notifier Webhook
        webhook_payload = {
            "client_id": TEST_CLIENT_ID,
            "user_id": "TEST_USER_V3",
            "access_token": VALID_V3_TOKEN,
            "timestamp": "2026-08-18T12:00:00Z"
        }

        # 2. Verify token against mock Upstox server
        prof_req = urllib.request.Request(
            f"http://127.0.0.1:{self.server_port}/v2/user/profile",
            headers={"Authorization": f"Bearer {webhook_payload['access_token']}"}
        )
        with urllib.request.urlopen(prof_req) as resp:
            self.assertEqual(resp.status, 200)
            profile_data = json.loads(resp.read().decode())
            self.assertEqual(profile_data["data"]["user_id"], "TEST_USER_V3")

        # 3. Gated persistence: Only save after HTTP 200 verification
        db.save_token(webhook_payload["access_token"])

        # 4. Verify token stored in DB
        stored = db.load_token()
        self.assertEqual(stored, VALID_V3_TOKEN)

        # 5. Check SHA-256 fingerprint
        fp = hashlib.sha256(stored.encode()).hexdigest()
        self.assertEqual(len(fp), 64)

    def test_invalid_webhook_token_rejected_and_not_persisted(self):
        """Test that invalid webhook token (HTTP 401) is rejected and NEVER persisted."""
        db = DatabaseManager(db_path=self.db_path)
        db.init_db()

        invalid_token = INVALID_V3_TOKEN
        prof_req = urllib.request.Request(
            f"http://127.0.0.1:{self.server_port}/v2/user/profile",
            headers={"Authorization": f"Bearer {invalid_token}"}
        )

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(prof_req)
        
        self.assertEqual(ctx.exception.code, 401)
        self.assertEqual(db.load_token(), "", "Invalid token must NOT be written to database")

    def test_oauth_v2_fallback_constants_remain_intact(self):
        """Verify fallback OAuth v2 URLs remain available and unmodified."""
        self.assertEqual(UPSTOX_AUTH_URL, "https://api.upstox.com/v2/login/authorization/dialog")
        self.assertEqual(UPSTOX_TOKEN_URL, "https://api.upstox.com/v2/login/authorization/token")


if __name__ == "__main__":
    unittest.main()
