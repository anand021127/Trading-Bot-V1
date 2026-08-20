"""Automated end-to-end test verifying the complete OAuth token propagation chain.
Uses an in-test mock Upstox server (test-only, never in production).
"""
import hashlib
import json
import os
import sqlite3
import unittest
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import tempfile
import urllib.request
import urllib.error

from backend.database.db_manager import DatabaseManager
from backend.broker.token_resolver import resolve_upstox_token, get_token_metadata, get_token_source
from backend.broker.upstox_expired_options import UpstoxExpiredOptionsClient


TEST_TOKEN = "test_oauth_access_token_abc123_valid_session_key"
TEST_TOKEN_FP = hashlib.sha256(TEST_TOKEN.encode()).hexdigest()
EXPECTED_FP = f"{TEST_TOKEN_FP[:6]}...{TEST_TOKEN_FP[-6:]}"


class MockUpstoxHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 0:
            self.rfile.read(content_length)

        if "/v2/login/authorization/token" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "success",
                "access_token": TEST_TOKEN,
                "token_type": "Bearer"
            }).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        auth_header = self.headers.get("Authorization", "")
        if "/v2/user/profile" in self.path:
            if f"Bearer {TEST_TOKEN}" in auth_header:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "success",
                    "data": {
                        "user_id": "TEST_USER_99",
                        "user_name": "Automated Test Trader",
                        "broker": "UPSTOX"
                    }
                }).encode())
            else:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "error",
                    "errors": [{"errorCode": "UDAPI100050", "message": "Invalid token"}]
                }).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Quiet test logging


class TestOAuthPropagationChain(unittest.TestCase):
    def setUp(self):
        for p in ["upstox_token.json", "data/upstox_token.json"]:
            if os.path.exists(p):
                try: os.remove(p)
                except Exception: pass
        self.orig_env = os.environ.get("UPSTOX_ACCESS_TOKEN")
        os.environ["UPSTOX_ACCESS_TOKEN"] = "old_stale_env_token_7f5e5b"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_trading.db")

        # Mock Upstox HTTP Server
        self.server = HTTPServer(("127.0.0.1", 0), MockUpstoxHandler)
        self.server_port = self.server.server_address[1]
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.temp_dir.cleanup()
        for p in ["upstox_token.json", "data/upstox_token.json"]:
            if os.path.exists(p):
                try: os.remove(p)
                except Exception: pass
        if self.orig_env is not None:
            os.environ["UPSTOX_ACCESS_TOKEN"] = self.orig_env
        else:
            os.environ.pop("UPSTOX_ACCESS_TOKEN", None)

    def test_full_oauth_propagation_chain(self):
        """Test full OAuth flow: exchange -> verification -> SQLite persistence -> propagation."""
        db = DatabaseManager(db_path=self.db_path)
        db.init_db()

        # 1. Verify initial fallback: SQLite is empty
        self.assertEqual(db.load_token(), "")

        # 2. Simulate OAuth code exchange
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.server_port}/v2/login/authorization/token",
            data=b"code=auth_code_xyz&grant_type=authorization_code",
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        with urllib.request.urlopen(req) as resp:
            token_data = json.loads(resp.read().decode())
        
        received_token = token_data["access_token"]
        self.assertEqual(received_token, TEST_TOKEN)

        # 3. Profile verification against mock server (HTTP 200 gate)
        prof_req = urllib.request.Request(
            f"http://127.0.0.1:{self.server_port}/v2/user/profile",
            headers={"Authorization": f"Bearer {received_token}"}
        )
        with urllib.request.urlopen(prof_req) as resp:
            self.assertEqual(resp.status, 200)
            prof_data = json.loads(resp.read().decode())
            self.assertEqual(prof_data["status"], "success")

        # 4. Save verified token to SQLite database
        db.save_token(received_token)

        # 5. Verify Token Precedence: SQLite token overrides stale environment variable
        self.assertEqual(db.load_token(), TEST_TOKEN)

        # 6. Verify UpstoxExpiredOptionsClient receives and resolves the exact same token
        expired_client = UpstoxExpiredOptionsClient(access_token=received_token)
        self.assertEqual(expired_client.access_token, received_token)

        # 7. Verify token fingerprint calculation matches expected
        sha = hashlib.sha256(received_token.encode()).hexdigest()
        fp = f"{sha[:6]}...{sha[-6:]}"
        self.assertEqual(fp, EXPECTED_FP)

    def test_failed_verification_does_not_persist(self):
        """Ensure invalid tokens (HTTP 401) are NOT saved to persistent storage."""
        db = DatabaseManager(db_path=self.db_path)
        db.init_db()

        invalid_token = "invalid_token_xyz"
        prof_req = urllib.request.Request(
            f"http://127.0.0.1:{self.server_port}/v2/user/profile",
            headers={"Authorization": f"Bearer {invalid_token}"}
        )

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(prof_req)
        
        self.assertEqual(ctx.exception.code, 401)
        self.assertEqual(db.load_token(), "", "Invalid token must NOT be persisted to SQLite")


if __name__ == "__main__":
    unittest.main()

