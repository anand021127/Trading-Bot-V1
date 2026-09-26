"""Control-plane auth tests (PHASE 5 item 29).

The control token is OPTIONAL by deployment policy: unset → no-op (paper/
local operation unchanged); set → state-changing endpoints require it.
The Upstox access token must never be accepted as the control token.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.control_auth import require_control_token


class TestControlAuth(unittest.TestCase):
    def _app(self) -> TestClient:
        """Attach the dependency exactly as production routers do:
        APIRouter(dependencies=[Depends(require_control_token)])."""
        from fastapi import APIRouter, Depends
        app = FastAPI()
        router = APIRouter(dependencies=[Depends(require_control_token)])

        @router.post("/protected")
        async def _protected():
            return {"ok": True}

        app.include_router(router)
        return TestClient(app)

    def test_unset_token_is_noop(self):
        with patch.dict(os.environ, {"CONTROL_TOKEN": ""}, clear=False):
            os.environ.pop("CONTROL_TOKEN", None)
            c = self._app()
            r = c.post("/protected")
            self.assertEqual(r.status_code, 200)

    def test_set_token_rejects_missing(self):
        with patch.dict(os.environ, {"CONTROL_TOKEN": "sekrit-token-1"}):
            c = self._app()
            r = c.post("/protected")
            self.assertEqual(r.status_code, 401)

    def test_set_token_rejects_wrong(self):
        with patch.dict(os.environ, {"CONTROL_TOKEN": "sekrit-token-1"}):
            c = self._app()
            r = c.post("/protected", headers={"X-Control-Token": "wrong"})
            self.assertEqual(r.status_code, 401)

    def test_correct_header_passes(self):
        with patch.dict(os.environ, {"CONTROL_TOKEN": "sekrit-token-1"}):
            c = self._app()
            r = c.post("/protected", headers={"X-Control-Token": "sekrit-token-1"})
            self.assertEqual(r.status_code, 200)

    def test_correct_bearer_passes(self):
        with patch.dict(os.environ, {"CONTROL_TOKEN": "sekrit-token-1"}):
            c = self._app()
            r = c.post("/protected", headers={"Authorization": "Bearer sekrit-token-1"})
            self.assertEqual(r.status_code, 200)

    def test_upstox_token_never_accepted_as_control_token(self):
        env = {"CONTROL_TOKEN": "real-control", "UPSTOX_ACCESS_TOKEN": "real-control"}
        with patch.dict(os.environ, env):
            c = self._app()
            r = c.post("/protected", headers={"X-Control-Token": "real-control"})
            # even if the values collide, the upstox guard rejects
            self.assertEqual(r.status_code, 401)


class TestRoutersUnchangedWhenUnset(unittest.TestCase):
    def test_bot_control_start_still_reachable_without_token(self):
        """Backward-compat guarantee: with CONTROL_TOKEN unset, protected
        routers behave exactly as in Phase 4."""
        os.environ.pop("CONTROL_TOKEN", None)
        from backend.api.routers.bot_control import router as bot_router
        app = FastAPI()
        app.include_router(bot_router, prefix="/api/bot")
        client = TestClient(app)
        r = client.get("/api/bot/status")
        self.assertNotEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
