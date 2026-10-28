"""The /api/v1 alias: one route table served under two prefixes (ADR §3.2)."""

from __future__ import annotations

import asyncio
import os
import unittest
import warnings
from unittest.mock import patch

from app.core.api_version import ApiVersionMiddleware


def _run(scope: dict) -> list[dict]:
    """Drive the middleware over a recorder app and return the scopes it saw."""
    captured: list[dict] = []

    async def recorder(received, receive, send):
        captured.append(received)

    asyncio.run(ApiVersionMiddleware(recorder)(scope, None, None))
    return captured


def _http(path: str, raw_path: bytes | None = None) -> dict:
    scope: dict = {"type": "http", "path": path}
    if raw_path is not None:
        scope["raw_path"] = raw_path
    return scope


def _app():
    from app.core.config import settings

    with patch.dict(os.environ, {"AUTH_ENABLED": "false"}), patch.object(settings, "AUTH_ENABLED_OVERRIDE", False):
        import app.main as main
    return main.app


class ApiVersionMiddlewareTests(unittest.TestCase):
    def test_versioned_path_is_rewritten_with_its_raw_path(self) -> None:
        (seen,) = _run(_http("/api/v1/x", b"/api/v1/x"))
        self.assertEqual(seen["path"], "/api/x")
        self.assertEqual(seen["raw_path"], b"/api/x")

    def test_bare_version_prefix_is_rewritten(self) -> None:
        (seen,) = _run(_http("/api/v1", b"/api/v1"))
        self.assertEqual(seen["path"], "/api")
        self.assertEqual(seen["raw_path"], b"/api")

    def test_paths_outside_the_prefix_are_untouched(self) -> None:
        for path in ("/api/v1x", "/api/v10/x", "/api/x", "/git/x"):
            with self.subTest(path=path):
                (seen,) = _run(_http(path))
                self.assertEqual(seen["path"], path)
                self.assertNotIn("api_version", seen.get("state", {}))

    def test_websocket_scopes_are_rewritten(self) -> None:
        (seen,) = _run({"type": "websocket", "path": "/api/v1/ws", "raw_path": b"/api/v1/ws"})
        self.assertEqual(seen["path"], "/api/ws")
        self.assertEqual(seen["raw_path"], b"/api/ws")
        self.assertEqual(seen["state"]["api_version"], "v1")

    def test_lifespan_scopes_pass_through(self) -> None:
        scope = {"type": "lifespan"}
        (seen,) = _run(scope)
        self.assertIs(seen, scope)
        self.assertNotIn("state", seen)

    def test_api_version_is_only_set_on_rewritten_scopes(self) -> None:
        (rewritten,) = _run(_http("/api/v1/x"))
        (plain,) = _run(_http("/api/x"))
        self.assertEqual(rewritten["state"]["api_version"], "v1")
        self.assertNotIn("state", plain)

    def test_the_original_scope_is_not_mutated(self) -> None:
        scope = _http("/api/v1/x", b"/api/v1/x")
        before = dict(scope)
        _run(scope)
        self.assertEqual(scope, before)
        self.assertNotIn("state", scope)


class RealAppVersionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app.core.config import settings

        cls.env = patch.dict(os.environ, {"AUTH_ENABLED": "false"})
        cls.auth = patch.object(settings, "AUTH_ENABLED_OVERRIDE", False)
        cls.env.start()
        cls.auth.start()
        cls.addClassCleanup(cls.auth.stop)
        cls.addClassCleanup(cls.env.stop)
        from fastapi.testclient import TestClient

        cls.app = _app()
        cls.client = TestClient(cls.app)

    def test_versioned_and_unversioned_routes_answer_alike(self) -> None:
        plain = self.client.get("/api/health/about")
        versioned = self.client.get("/api/v1/health/about")
        self.assertEqual(plain.status_code, 200, plain.text)
        self.assertEqual(versioned.status_code, 200, versioned.text)
        self.assertEqual(versioned.json(), plain.json())

    def test_versioned_openapi_is_served(self) -> None:
        response = self.client.get("/api/v1/openapi.json")
        self.assertEqual(response.status_code, 200, response.text)

    def test_openapi_paths_are_versioned_and_api_only(self) -> None:
        paths = self.client.get("/api/v1/openapi.json").json()["paths"]
        self.assertTrue(paths)
        for path in paths:
            self.assertTrue(path.startswith("/api/v1/"), path)
            self.assertFalse(path.startswith("/git"), path)

    def test_operation_ids_are_unique_and_emit_no_duplicate_warning(self) -> None:
        previous = self.app.openapi_schema
        self.app.openapi_schema = None
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                schema = self.app.openapi()
        finally:
            self.app.openapi_schema = previous

        ids = [
            operation["operationId"]
            for item in schema["paths"].values()
            for method, operation in item.items()
            if method in ("get", "put", "post", "delete", "patch")
        ]
        self.assertEqual(len(ids), len(set(ids)), "operationIds are not unique")

        duplicates = [str(warning.message) for warning in caught if "Duplicate Operation ID" in str(warning.message)]
        self.assertEqual(duplicates, [])


if __name__ == "__main__":
    unittest.main()
