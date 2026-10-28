"""The /api/admin/service-clients route accepts only known scopes.

Drives the real app with a signed-in admin session. The catalog schema lives in
PRISM_DATABASE_URL, so the class is skipped without it.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import policy  # noqa: E402
from app.core.config import settings  # noqa: E402

DATABASE_URL = os.environ.get("PRISM_DATABASE_URL", "").strip()

# Starlette's TestClient needs an HTTP client package (httpx2, or httpx) that the
# locked runtime does not ship.
try:
    import starlette.testclient  # noqa: F401
    HAS_TEST_CLIENT = True
except RuntimeError:
    HAS_TEST_CLIENT = False

TEST_SECRET = "test-secret-at-least-32-characters-long-x"


@unittest.skipUnless(DATABASE_URL, "PRISM_DATABASE_URL is required for service-client route tests")
@unittest.skipUnless(HAS_TEST_CLIENT, "starlette.testclient needs httpx2 or httpx")
class ServiceClientScopeRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from starlette.testclient import TestClient

        cls._patchers = [
            patch.object(settings, "AUTH_ENABLED_OVERRIDE", True),
            patch.object(settings, "PASSWORD_AUTH_ENABLED", True),
            patch.object(settings, "SESSION_SECRET", TEST_SECRET),
        ]
        for patcher in cls._patchers:
            patcher.start()

        import app.main as main
        from app.services import access_service, service_client_service, session_store_service

        session_store_service.initialize_session_store()
        cls.main = main
        cls.access_service = access_service
        cls.service_client_service = service_client_service
        cls.session_store_service = session_store_service
        cls.TestClient = TestClient

    @classmethod
    def tearDownClass(cls) -> None:
        for patcher in getattr(cls, "_patchers", []):
            patcher.stop()

    def _admin_client(self):
        from app.core.session import SESSION_COOKIE_NAME, create_session_token

        email = "service-client-admin@example.com"
        self.access_service.upsert_user(email=email, name="Service Client Admin")
        self.access_service.upsert_user_role(email, "admin", updated_by=email)
        session_id, _ = self.session_store_service.create_session(email=email, name="Service Client Admin", picture="")
        client = self.TestClient(self.main.app)
        client.cookies.set(SESSION_COOKIE_NAME, create_session_token(session_id))
        return client

    def test_an_unknown_scope_is_rejected(self) -> None:
        response = self._admin_client().post(
            "/api/admin/service-clients",
            json={"name": "PLM", "scopes": ["api:read", "nope"]},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("nope", response.text)

    def test_every_area_scope_and_wildcard_starts_a_client(self) -> None:
        client = self._admin_client()
        for scope in (*policy.AREA_WRITE_SCOPES, "*"):
            with self.subTest(scope=scope):
                response = client.post(
                    "/api/admin/service-clients",
                    json={"name": f"PLM {scope}", "role": "viewer", "scopes": [scope]},
                )
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(body["scopes"], [scope])
                self.addCleanup(self.service_client_service.delete_service_client, body["client_id"])

    def test_the_typed_clients_keep_their_public_body_and_secret_once(self) -> None:
        client = self._admin_client()
        created = client.post(
            "/api/admin/service-clients",
            json={"name": "PLM typed", "role": "viewer", "scopes": ["api:read"]},
        ).json()
        self.addCleanup(self.service_client_service.delete_service_client, created["client_id"])
        self.assertEqual(
            {key: type(value).__name__ for key, value in created.items()},
            {
                "client_id": "str",
                "name": "str",
                "role": "str",
                "scopes": "list",
                "enabled": "bool",
                "created_at": "str",
                "updated_at": "str",
                "last_used_at": "NoneType",
                "client_secret": "str",
            },
        )
        self.assertEqual(created["name"], "PLM typed")
        self.assertTrue(created["client_secret"].startswith("kp_sc_"))

        listed = client.get("/api/admin/service-clients")
        self.assertEqual(listed.status_code, 200, listed.text)
        item = next(entry for entry in listed.json()["items"] if entry["client_id"] == created["client_id"])
        self.assertEqual(item, {key: value for key, value in created.items() if key != "client_secret"})

        rotated = client.post(f"/api/admin/service-clients/{created['client_id']}/rotate-secret")
        self.assertEqual(rotated.status_code, 200, rotated.text)
        self.assertNotEqual(rotated.json()["client_secret"], created["client_secret"])
        rotated_body = rotated.json()
        self.assertEqual(
            {key: value for key, value in rotated_body.items() if key not in {"client_secret", "updated_at"}},
            {key: value for key, value in item.items() if key != "updated_at"},
        )

        patched = client.patch(
            f"/api/admin/service-clients/{created['client_id']}", json={"enabled": False}
        )
        self.assertEqual(patched.status_code, 200, patched.text)
        self.assertEqual(patched.json(), {**item, "enabled": False, "updated_at": patched.json()["updated_at"]})

        deleted = client.delete(f"/api/admin/service-clients/{created['client_id']}")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json(), {"ok": True})
        remaining = client.get("/api/admin/service-clients").json()["items"]
        self.assertNotIn(created["client_id"], {entry["client_id"] for entry in remaining})


if __name__ == "__main__":
    unittest.main()
