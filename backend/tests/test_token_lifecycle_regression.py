"""Regression test suite for Upstox token lifecycle and expired options API client.

Proves:
1. The exact verified token from validate_token_live reaches UpstoxExpiredOptionsClient
   and its HTTP Authorization header.
2. Fresh JWT tokens from environment/dotenv take strict precedence over mock or stale DB tokens.
3. DatabaseManager.save_token() protects runtime environment from mock test tokens.
4. Token strings with whitespace or surrounding quotes are stripped consistently across all components.
"""
import os
import json
import base64
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from backend.broker.token_resolver import (
    resolve_upstox_token,
    resolve_upstox_token_with_source,
    get_token_source,
    get_token_metadata,
    validate_token_live,
    token_fingerprint,
    persist_upstox_token,
)
from backend.broker.upstox_expired_options import (
    UpstoxExpiredOptionsClient,
    OptionsDataCache,
)
from backend.database.db_manager import DatabaseManager
from scripts.download_historical_options import (
    HistoricalOptionsIngestionPipeline,
    ContractRequirement,
)


def _make_dummy_jwt(payload: dict) -> str:
    h = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).decode().rstrip("=")
    p = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    s = base64.urlsafe_b64encode(b"sig123456789012345678901234567890").decode().rstrip("=")
    return f"{h}.{p}.{s}"


class TestTokenLifecycleRegression(unittest.TestCase):

    def setUp(self):
        self.original_env_token = os.environ.get("UPSTOX_ACCESS_TOKEN")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_trading_bot.db")
        self.cache_dir = os.path.join(self.temp_dir.name, "options_cache")
        os.makedirs(self.cache_dir, exist_ok=True)

    def tearDown(self):
        if self.original_env_token is not None:
            os.environ["UPSTOX_ACCESS_TOKEN"] = self.original_env_token
        else:
            os.environ.pop("UPSTOX_ACCESS_TOKEN", None)
        self.temp_dir.cleanup()

    def test_verified_token_reaches_expired_options_client_and_pipeline(self):
        """Proves that a token verified by validate_token_live matches the client's token and header."""
        jwt_token = _make_dummy_jwt({"user_id": "U12345", "exp": 9999999999, "isPlusPlan": True})
        expected_fp = token_fingerprint(jwt_token)

        # Mock Upstox live profile and expired expiries endpoints
        profile_response = json.dumps({
            "status": "success",
            "data": {
                "user_id": "U12345",
                "user_name": "Test Trader",
                "email": "test@example.com",
                "user_type": "individual",
            }
        }).encode("utf-8")

        expired_response = json.dumps({
            "status": "success",
            "data": ["2024-06-27", "2024-07-25"]
        }).encode("utf-8")

        def mock_urlopen(req, timeout=None, context=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            headers = req.headers if hasattr(req, "headers") else {}
            auth_header = headers.get("Authorization") or headers.get("authorization")

            # Assert header contains the exact verified token
            self.assertEqual(auth_header, f"Bearer {jwt_token}")

            mock_resp = MagicMock()
            mock_resp.__enter__.return_value = mock_resp
            mock_resp.status = 200
            if "user/profile" in url:
                mock_resp.read.return_value = profile_response
            elif "expired-instruments" in url:
                mock_resp.read.return_value = expired_response
            else:
                mock_resp.read.return_value = b'{"status":"success"}'
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            # 1. Live validation
            val_res = validate_token_live(token=jwt_token)
            self.assertTrue(val_res["valid"])
            self.assertTrue(val_res["profile_verified"])
            self.assertTrue(val_res["expired_instruments_entitled"])
            self.assertTrue(val_res["accessible"])
            self.assertEqual(val_res["profile_status"], 200)
            self.assertEqual(val_res["expired_instruments_status"], 200)
            self.assertEqual(val_res["token_fingerprint"], expected_fp)

            # 2. Pipeline initialization with same token
            pipeline = HistoricalOptionsIngestionPipeline(
                access_token=jwt_token,
                cache_dir=self.cache_dir,
            )

            # Assert client has exact token and fingerprint
            self.assertEqual(pipeline.client.access_token, jwt_token)
            client_fp = token_fingerprint(pipeline.client.access_token)
            self.assertEqual(client_fp, expected_fp)

            # Assert client headers match
            headers = pipeline.client._headers()
            self.assertEqual(headers["Authorization"], f"Bearer {jwt_token}")
            self.assertIn("Upstox", headers["User-Agent"])

            # 3. Pipeline preflight auth
            auth_res = pipeline.test_auth()
            self.assertTrue(auth_res["accessible"])
            self.assertEqual(auth_res["token_fingerprint"], expected_fp)

            # 4. Expired client call propagates the identical token and authorization header
            expiries = pipeline.client.get_expiries("NIFTY50")
            self.assertEqual(expiries, ["2024-06-27", "2024-07-25"])

    def test_real_jwt_prioritized_over_mock_or_stale_database_token(self):
        """Proves that a real JWT in os.environ is chosen over a mock token stored in DB."""
        real_jwt = _make_dummy_jwt({"user_id": "U99999", "exp": 9999999999})
        os.environ["UPSTOX_ACCESS_TOKEN"] = real_jwt

        db = DatabaseManager(db_path=self.db_path)
        # Store mock token in DB
        db.save_setting("upstox_access_token", "mock-fresh-access-token-456")

        with patch("backend.database.db_manager.DatabaseManager", return_value=db):
            resolved = resolve_upstox_token()
            self.assertEqual(resolved, real_jwt)
            src = get_token_source()
            self.assertEqual(src, "environment (os.environ)")

    def test_save_token_does_not_clobber_environment_with_mock(self):
        """Proves DatabaseManager.save_token does not overwrite os.environ with mock strings."""
        real_jwt = _make_dummy_jwt({"user_id": "U55555", "exp": 9999999999})
        os.environ["UPSTOX_ACCESS_TOKEN"] = real_jwt

        db = DatabaseManager(db_path=self.db_path)
        db.save_token("mock-test-token-only")

        # Database setting is saved for test purposes
        self.assertEqual(db.load_token(), "mock-test-token-only")

        # But os.environ was NOT corrupted
        self.assertEqual(os.environ.get("UPSTOX_ACCESS_TOKEN"), real_jwt)

    def test_token_stripping_and_quotes_handling(self):
        """Proves that surrounding quotes and whitespace are cleanly stripped across all interfaces."""
        inner_token = _make_dummy_jwt({"user_id": "U77777", "exp": 9999999999})
        quoted_token = f'  "{inner_token}"  \n'

        cleaned_resolved = resolve_upstox_token(explicit_token=quoted_token)
        self.assertEqual(cleaned_resolved, inner_token)

        client = UpstoxExpiredOptionsClient(access_token=quoted_token, cache_dir=self.cache_dir)
        self.assertEqual(client.access_token, inner_token)
        self.assertEqual(client._headers()["Authorization"], f"Bearer {inner_token}")


if __name__ == "__main__":
    unittest.main()
