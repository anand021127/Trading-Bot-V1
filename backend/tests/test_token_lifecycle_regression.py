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
        from backend.broker.token_resolver import clear_verified_runtime_token
        self.original_env_token = os.environ.get("UPSTOX_ACCESS_TOKEN")
        os.environ.pop("UPSTOX_ACCESS_TOKEN", None)
        clear_verified_runtime_token()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_trading_bot.db")
        self.cache_dir = os.path.join(self.temp_dir.name, "options_cache")
        os.makedirs(self.cache_dir, exist_ok=True)

    def tearDown(self):
        from backend.broker.token_resolver import clear_verified_runtime_token
        clear_verified_runtime_token()
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

    def test_database_token_overwrite_protection_prevents_stale_token_resurrection(self):
        """Proves that DatabaseManager.save_token rejects stale/expired tokens when an active token exists."""
        db = DatabaseManager(db_path=self.db_path)

        # 1. Save an active verified token
        active_jwt = _make_dummy_jwt({"user_id": "U100", "iat": 1700000000, "exp": 9999999999})
        saved = db.save_token(active_jwt, verified=True, source="oauth_callback")
        self.assertTrue(saved)
        self.assertEqual(db.load_token(require_valid=True), active_jwt)

        # 2. Attempt to overwrite with an expired token
        expired_jwt = _make_dummy_jwt({"user_id": "U100", "iat": 1600000000, "exp": 1600001000})
        saved_expired = db.save_token(expired_jwt, verified=False, source="stale_source")
        self.assertFalse(saved_expired)
        # Active token still intact
        self.assertEqual(db.load_token(require_valid=True), active_jwt)

        # 3. Attempt to overwrite with an older iat unverified token
        older_jwt = _make_dummy_jwt({"user_id": "U100", "iat": 1690000000, "exp": 9999999999})
        saved_older = db.save_token(older_jwt, verified=False, source="legacy_import")
        self.assertFalse(saved_older)
        self.assertEqual(db.load_token(require_valid=True), active_jwt)

        # 4. Overwrite with a fresher verified token succeeds
        fresher_jwt = _make_dummy_jwt({"user_id": "U100", "iat": 1750000000, "exp": 9999999999})
        saved_fresher = db.save_token(fresher_jwt, verified=True, source="oauth_callback")
        self.assertTrue(saved_fresher)
        self.assertEqual(db.load_token(require_valid=True), fresher_jwt)

    def test_tiered_scoring_priority_enforces_authoritative_resolution(self):
        """Proves strict resolution priority: Runtime Verified > Persisted Verified > Others."""
        from backend.broker.token_resolver import (
            set_verified_runtime_token,
            clear_verified_runtime_token,
            resolve_upstox_token_with_source,
        )

        db = DatabaseManager(db_path=self.db_path)
        persisted_verified = _make_dummy_jwt({"user_id": "U_DB", "iat": 1700000000, "exp": 9999999999})
        db.save_token(persisted_verified, verified=True, source="database (SQLite verified)")

        # When runtime verified token is set, it takes priority
        runtime_verified = _make_dummy_jwt({"user_id": "U_RUN", "iat": 1710000000, "exp": 9999999999})
        set_verified_runtime_token(runtime_verified, {"verified": True, "source": "runtime (in-memory verified)"})

        with patch("backend.database.db_manager.DatabaseManager", return_value=db), \
             patch("backend.broker.token_resolver.find_repo_dotenv_path", return_value=None):
            resolved, src = resolve_upstox_token_with_source(require_valid=True)
            self.assertEqual(resolved, runtime_verified)
            self.assertEqual(src, "runtime (in-memory verified)")

            # When runtime verified is cleared, persisted verified wins
            clear_verified_runtime_token()
            resolved2, src2 = resolve_upstox_token_with_source(require_valid=True)
            self.assertEqual(resolved2, persisted_verified)
            self.assertIn("verified", src2)

    def test_client_immutability_and_no_reresolution(self):
        """Proves UpstoxExpiredOptionsClient preserves the passed access_token immutably."""
        explicit_tok = _make_dummy_jwt({"user_id": "U_IMMUTABLE", "exp": 9999999999})
        os.environ["UPSTOX_ACCESS_TOKEN"] = "stale-env-token-should-not-be-used"

        client = UpstoxExpiredOptionsClient(access_token=explicit_tok, cache_dir=self.cache_dir)
        self.assertEqual(client.access_token, explicit_tok)
        self.assertEqual(client.token_source, "explicit_runtime")
        self.assertEqual(client._headers()["Authorization"], f"Bearer {explicit_tok}")

    def test_failsafe_abort_when_no_active_token(self):
        """Proves resolve_upstox_token(require_valid=True) returns empty when only expired tokens exist."""
        from backend.broker.token_resolver import clear_verified_runtime_token
        clear_verified_runtime_token()
        os.environ.pop("UPSTOX_ACCESS_TOKEN", None)

        expired_tok = _make_dummy_jwt({"user_id": "U_DEAD", "exp": 1500000000})
        db = DatabaseManager(db_path=self.db_path)
        # Bypassing protection to force-store an expired token
        db.save_setting("upstox_access_token", expired_tok)

        with patch("backend.database.db_manager.DatabaseManager", return_value=db), \
             patch("backend.broker.token_resolver.find_repo_dotenv_path", return_value=None):
            resolved, src = resolve_upstox_token_with_source(require_valid=True)
            self.assertEqual(resolved, "")
            self.assertEqual(src, "none")

    def test_check_token_freshness_lifecycle(self):
        """Proves check_token_freshness accurately parses exp and classifies fresh vs expired."""
        import time
        from backend.broker.token_resolver import check_token_freshness

        # 1. Fresh token (future exp)
        fresh_jwt = _make_dummy_jwt({"user_id": "U_FRESH", "exp": time.time() + 86400})
        freshness = check_token_freshness(fresh_jwt)
        self.assertTrue(freshness["is_fresh"])
        self.assertFalse(freshness["is_expired"])
        self.assertEqual(freshness["status"], "FRESH")

        # 2. Expired token (past exp)
        expired_jwt = _make_dummy_jwt({"user_id": "U_EXPIRED", "exp": time.time() - 3600})
        freshness_exp = check_token_freshness(expired_jwt)
        self.assertFalse(freshness_exp["is_fresh"])
        self.assertTrue(freshness_exp["is_expired"])
        self.assertEqual(freshness_exp["status"], "EXPIRED")

        # 3. Expiring soon (< 300s)
        soon_jwt = _make_dummy_jwt({"user_id": "U_SOON", "exp": time.time() + 120})
        freshness_soon = check_token_freshness(soon_jwt)
        self.assertTrue(freshness_soon["is_fresh"])
        self.assertFalse(freshness_soon["is_expired"])
        self.assertEqual(freshness_soon["status"], "EXPIRING_SOON")

    def test_validate_token_live_error_classification(self):
        """Proves validate_token_live distinguishes 401 AUTHENTICATION_FAILURE from EXPIRED_OPTIONS_ENTITLEMENT_FAILURE."""
        import urllib.error
        valid_jwt = _make_dummy_jwt({"user_id": "U_TEST", "exp": 9999999999})

        # Scenario A: 401 Unauthorized from /v2/user/profile -> AUTHENTICATION_FAILURE
        def mock_401_urlopen(req, timeout=None, context=None):
            raise urllib.error.HTTPError(
                url="https://api.upstox.com/v2/user/profile",
                code=401,
                msg="Unauthorized",
                hdrs={},
                fp=None,
            )

        with patch("urllib.request.urlopen", side_effect=mock_401_urlopen):
            res = validate_token_live(valid_jwt)
            self.assertFalse(res["valid"])
            self.assertFalse(res["profile_verified"])
            self.assertEqual(res["failure_classification"], "AUTHENTICATION_FAILURE")
            self.assertEqual(res["error_code"], "AUTHENTICATION_FAILURE")
            self.assertEqual(res["profile_status"], 401)

        # Scenario B: 200 Profile + 403 Expired Options -> EXPIRED_OPTIONS_ENTITLEMENT_FAILURE
        def mock_entitlement_fail_urlopen(req, timeout=None, context=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "user/profile" in url:
                mock_resp = MagicMock()
                mock_resp.__enter__.return_value = mock_resp
                mock_resp.status = 200
                mock_resp.read.return_value = json.dumps({
                    "status": "success",
                    "data": {"user_id": "U_TRADER", "user_name": "Pro Trader"}
                }).encode("utf-8")
                return mock_resp
            else:
                raise urllib.error.HTTPError(
                    url="https://api.upstox.com/v2/expired-instruments/expiries",
                    code=403,
                    msg="Forbidden",
                    hdrs={},
                    fp=None,
                )

        with patch("urllib.request.urlopen", side_effect=mock_entitlement_fail_urlopen):
            res_b = validate_token_live(valid_jwt)
            self.assertTrue(res_b["valid"])
            self.assertTrue(res_b["profile_verified"])
            self.assertFalse(res_b["expired_instruments_entitled"])
            self.assertEqual(res_b["failure_classification"], "EXPIRED_OPTIONS_ENTITLEMENT_FAILURE")

    def test_dynamic_token_resolution_across_clients(self):
        """Proves dynamic resolution across clients with proper isolation from leaked verified state.

        Coverage:
        a) Clean environment + tok1 via env (no verified/DB/dotenv leakage)
        b) Stale verified runtime state overridden by explicit test canonical setup
        c) invalidate_old_token_references(tok1 -> tok2) propagates to non-explicit clients
        d) Explicit access_token clients remain explicit and are not overwritten
        """
        from backend.broker.upstox_client import UpstoxClient
        from backend.broker.token_resolver import (
            invalidate_old_token_references,
            set_verified_runtime_token,
            clear_verified_runtime_token,
            get_verified_runtime_token,
        )

        tok1 = _make_dummy_jwt({"user_id": "U1", "exp": 9999999999})
        tok2 = _make_dummy_jwt({"user_id": "U2", "exp": 9999999999})
        stale = _make_dummy_jwt({"user_id": "U_STALE", "exp": 9999999999})

        # Isolate resolver from persisted DB / dotenv / json so only env + runtime verified matter.
        empty_db = DatabaseManager(db_path=self.db_path)
        isolation = (
            patch("backend.database.db_manager.DatabaseManager", return_value=empty_db),
            patch("backend.broker.token_resolver.find_repo_dotenv_path", return_value=None),
            patch(
                "backend.broker.token_resolver.get_token_diagnostic_candidates",
                side_effect=lambda explicit_token=None, dotenv_path=None: (
                    # Rebuild candidates from runtime verified + env only (no disk leakage).
                    _isolated_candidates(explicit_token)
                ),
            ),
        )

        def _isolated_candidates(explicit_token=None):
            from backend.broker.token_resolver import (
                get_verified_runtime_token as _gvt,
                token_fingerprint as _fp,
                decode_jwt_safe as _dj,
            )
            results = []
            if explicit_token and str(explicit_token).strip():
                tok = str(explicit_token).strip().strip("\"'").strip()
                jwt = _dj(tok)
                results.append({
                    "source": "runtime (--token)",
                    "source_key": "runtime",
                    "token": tok,
                    "fingerprint": _fp(tok),
                    "length": len(tok),
                    "is_jwt": jwt.get("is_jwt", False),
                    "issued_at_iso": jwt.get("issued_at_iso"),
                    "expires_at_iso": jwt.get("expires_at_iso"),
                    "is_expired": jwt.get("is_expired"),
                    "isPlusPlan": jwt.get("isPlusPlan", False),
                    "verified": True,
                    "rejection_reason": "ACTIVE_SELECTION",
                })
                return results
            v_tok = _gvt()
            if v_tok and v_tok.get("token"):
                tok = v_tok["token"]
                jwt = _dj(tok)
                results.append({
                    "source": v_tok.get("source", "runtime (verified)"),
                    "source_key": "runtime",
                    "token": tok,
                    "fingerprint": _fp(tok),
                    "length": len(tok),
                    "is_jwt": jwt.get("is_jwt", False),
                    "issued_at_iso": jwt.get("issued_at_iso"),
                    "expires_at_iso": jwt.get("expires_at_iso"),
                    "is_expired": jwt.get("is_expired"),
                    "isPlusPlan": jwt.get("isPlusPlan", False) or v_tok.get("is_plus_plan", False),
                    "verified": True,
                    "rejection_reason": "ACTIVE_SELECTION",
                })
            env_token = (os.getenv("UPSTOX_ACCESS_TOKEN") or "").strip().strip("\"'").strip()
            if env_token:
                jwt = _dj(env_token)
                results.append({
                    "source": "environment (os.environ)",
                    "source_key": "environment",
                    "token": env_token,
                    "fingerprint": _fp(env_token),
                    "length": len(env_token),
                    "is_jwt": jwt.get("is_jwt", False),
                    "issued_at_iso": jwt.get("issued_at_iso"),
                    "expires_at_iso": jwt.get("expires_at_iso"),
                    "is_expired": jwt.get("is_expired"),
                    "isPlusPlan": jwt.get("isPlusPlan", False),
                    "verified": False,
                    "rejection_reason": "EXPIRED" if jwt.get("is_expired") is True else None,
                })
            return results

        # ---- (a) clean environment + tok1 ----
        clear_verified_runtime_token()
        os.environ["UPSTOX_ACCESS_TOKEN"] = tok1
        with isolation[0], isolation[1], isolation[2]:
            client = UpstoxClient()
            expired_client = UpstoxExpiredOptionsClient(cache_dir=self.cache_dir)
            self.assertEqual(client.access_token, tok1)
            self.assertEqual(expired_client.access_token, tok1)

            # ---- (c) invalidate tok1 -> tok2 propagates to non-explicit clients ----
            invalidate_old_token_references(tok2)
            self.assertEqual(client.access_token, tok2)
            self.assertEqual(expired_client.access_token, tok2)
            self.assertEqual(os.environ.get("UPSTOX_ACCESS_TOKEN"), tok2)
            v = get_verified_runtime_token()
            self.assertIsNotNone(v)
            self.assertEqual(v.get("token"), tok2)

        # ---- (b) existing stale verified runtime + explicit test setup ----
        clear_verified_runtime_token()
        set_verified_runtime_token(
            stale,
            {"source": "runtime (stale verified)", "verified_at": "2020-01-01T00:00:00Z"},
        )
        os.environ["UPSTOX_ACCESS_TOKEN"] = tok1  # env alone must NOT beat verified
        with isolation[0], isolation[1], isolation[2]:
            # Production priority: verified runtime wins over arbitrary env
            leaked_client = UpstoxClient()
            self.assertEqual(leaked_client.access_token, stale)

            # Explicit test setup establishes tok1 as the new canonical verified token
            invalidate_old_token_references(tok1)
            setup_client = UpstoxClient()
            setup_expired = UpstoxExpiredOptionsClient(cache_dir=self.cache_dir)
            self.assertEqual(setup_client.access_token, tok1)
            self.assertEqual(setup_expired.access_token, tok1)
            self.assertEqual(os.environ.get("UPSTOX_ACCESS_TOKEN"), tok1)

            # ---- (d) explicit access_token clients remain explicit ----
            explicit_client = UpstoxClient(access_token=tok1)
            explicit_expired = UpstoxExpiredOptionsClient(
                access_token=tok1, cache_dir=self.cache_dir
            )
            self.assertEqual(explicit_client.access_token, tok1)
            self.assertEqual(explicit_expired.access_token, tok1)

            invalidate_old_token_references(tok2)
            # Non-explicit clients pick up tok2
            self.assertEqual(setup_client.access_token, tok2)
            self.assertEqual(setup_expired.access_token, tok2)
            # Explicit clients are NOT overwritten
            self.assertEqual(explicit_client.access_token, tok1)
            self.assertEqual(explicit_expired.access_token, tok1)
            self.assertEqual(os.environ.get("UPSTOX_ACCESS_TOKEN"), tok2)


if __name__ == "__main__":
    unittest.main()
