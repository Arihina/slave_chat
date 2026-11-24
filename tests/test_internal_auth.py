"""Контрактные тесты внутренней аутентификации master -> агент.

Соответствуют docs/agent-side-changes.md, раздел 6.7. Проверяем middleware
на минимальном Starlette-приложении, без БД и Ollama.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.platform_security import InternalAuthMiddleware

TOKEN = "c0ffee" * 8  # 48 символов, как реальный per-agent секрет
USER = "123e4567-e89b-12d3-a456-426614174000"


def make_app(**overrides) -> Starlette:
    defaults = {
        "public_paths": ["/health"],
        "internal_auth_mode": "token",
        "master_internal_token": TOKEN,
        "agent_id": "chat",
    }
    defaults.update(overrides)
    settings = SimpleNamespace(**defaults)

    async def health(request):
        return JSONResponse({"status": "ok"})

    async def business(request):
        return JSONResponse({"user_id": str(request.state.user_id)})

    app = Starlette(
        routes=[Route("/health", health), Route("/v1/test", business)]
    )
    app.add_middleware(InternalAuthMiddleware, settings=settings)
    return app


class InternalAuthTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(make_app())

    def _ok_headers(self):
        return {
            "X-Internal-Token": TOKEN,
            "X-User-Id": USER,
        }

    def test_no_token_returns_401(self):
        resp = self.client.get("/v1/test", headers={"X-User-Id": USER})
        self.assertEqual(resp.status_code, 401)

    def test_wrong_token_returns_401(self):
        resp = self.client.get(
            "/v1/test",
            headers={"X-Internal-Token": "wrong", "X-User-Id": USER},
        )
        self.assertEqual(resp.status_code, 401)

    def test_valid_token_valid_user_returns_200(self):
        resp = self.client.get("/v1/test", headers=self._ok_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["user_id"], USER)

    def test_valid_token_missing_user_returns_401(self):
        resp = self.client.get(
            "/v1/test", headers={"X-Internal-Token": TOKEN}
        )
        self.assertEqual(resp.status_code, 401)

    def test_valid_token_bad_user_returns_401(self):
        resp = self.client.get(
            "/v1/test",
            headers={"X-Internal-Token": TOKEN, "X-User-Id": "not-a-uuid"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_health_without_token_returns_200(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})

    def test_prefix_health_is_not_public(self):
        # /healthz (или /health/../admin) не должен попасть в allowlist.
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 401)

    def test_request_id_echoed(self):
        resp = self.client.get(
            "/v1/test",
            headers={**self._ok_headers(), "X-Request-Id": "abc.123"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("X-Request-Id"), "abc.123")

    def test_invalid_request_id_replaced(self):
        resp = self.client.get(
            "/v1/test",
            headers={**self._ok_headers(), "X-Request-Id": "bad id\n"},
        )
        self.assertEqual(resp.status_code, 200)
        # Некорректный id не даёт 400, а заменяется новым (32 hex).
        self.assertRegex(resp.headers.get("X-Request-Id", ""), r"^[0-9a-f]{32}$")

    def test_error_body_platform_format(self):
        resp = self.client.get("/v1/test", headers={"X-User-Id": USER})
        body = resp.json()
        self.assertEqual(body["error"]["type"], "authentication_error")
        self.assertIsNone(body["error"]["param"])
        self.assertIsNone(body["error"]["code"])


class OffModeTests(unittest.TestCase):
    def test_mode_off_skips_token_but_validates_user(self):
        client = TestClient(make_app(internal_auth_mode="off"))
        resp = client.get("/v1/test", headers={"X-User-Id": USER})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["user_id"], USER)

    def test_mode_off_still_requires_valid_user(self):
        client = TestClient(make_app(internal_auth_mode="off"))
        resp = client.get("/v1/test", headers={"X-User-Id": "nope"})
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
