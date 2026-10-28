"""Admin routes for outbound webhooks (ADR §3.10).

The tests drive the real application with personal access tokens: an admin's
token may read and write registrations, a viewer's token is refused by
``require_admin``, and no response except create and rotate-secret carries the
signing secret.
"""

from __future__ import annotations

import ipaddress
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.services import access_service, personal_tokens  # noqa: E402
from app.services.webhooks import delivery, targets  # noqa: E402

DATABASE_URL = os.environ.get("PRISM_DATABASE_URL", "").strip()

try:
    import starlette.testclient  # noqa: F401

    HAS_TEST_CLIENT = True
except RuntimeError:
    HAS_TEST_CLIENT = False

TEST_SECRET = "test-secret-at-least-32-characters-long-x"
ROOT_KEY = "cc" * 32
BASE = "https://prism.test"


def resolves_public(_host: str, _port: int):
    return [ipaddress.ip_address("93.184.216.34")]


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@unittest.skipUnless(DATABASE_URL, "PRISM_DATABASE_URL is required for the webhook route tests")
@unittest.skipUnless(HAS_TEST_CLIENT, "starlette.testclient needs httpx2 or httpx")
class WebhookRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from starlette.testclient import TestClient

        cls._patchers = [
            patch.object(settings, "AUTH_ENABLED_OVERRIDE", True),
            patch.object(settings, "PASSWORD_AUTH_ENABLED", True),
            patch.object(settings, "SESSION_SECRET", TEST_SECRET),
            patch.object(settings, "PUBLIC_BASE_URL", BASE),
            patch.object(settings, "TRACKER_CREDENTIAL_ROOT_KEY", SecretStr(ROOT_KEY)),
            patch.object(settings, "TRACKER_CREDENTIAL_ROOT_KEY_ID", "v1"),
            patch.object(settings, "TRACKER_CREDENTIAL_PREVIOUS_ROOT_KEY", SecretStr("")),
            patch.object(settings, "TRACKER_CREDENTIAL_PREVIOUS_ROOT_KEY_ID", ""),
            patch.object(settings, "PRISM_WEBHOOK_ALLOW_PRIVATE", False),
        ]
        for patcher in cls._patchers:
            patcher.start()

        import app.main as main

        cls.main = main
        cls.TestClient = TestClient

    @classmethod
    def tearDownClass(cls) -> None:
        for patcher in getattr(cls, "_patchers", []):
            patcher.stop()

    def setUp(self) -> None:
        self.client = self.TestClient(self.main.app)
        self._emails: list[str] = []
        resolver = patch.object(targets, "resolve_addresses", resolves_public)
        resolver.start()
        self.addCleanup(resolver.stop)
        self.addCleanup(self._cleanup)

        self.viewer_email, token, _token_id = self._user("viewer")
        self.admin_email, admin_token, _ = self._user("admin")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.admin_headers = {"Authorization": f"Bearer {admin_token}"}

    def _user(self, role: str) -> tuple[str, str, str]:
        email = f"{unique('webhook-route')}@example.test"
        access_service.upsert_user(email=email, name=email.split("@")[0])
        access_service.upsert_user_role(email, role, updated_by=email)
        scopes = ["admin"] if role == "admin" else ["api:read"]
        row, token = personal_tokens.create(email, "webhook route tests", scopes, 30)
        self._emails.append(email)
        return email, token, str(row["id"])

    def _cleanup(self) -> None:
        from app.services.postgres_database import database

        with database.connection() as conn:
            conn.execute("SET search_path TO workspace, public")
            conn.execute(
                "DELETE FROM ws_webhooks WHERE created_by = ANY(%s)", (self._emails,)
            )

    def create(self, **overrides) -> dict:
        payload = {
            "url": "https://hooks.example.test/prism",
            "eventKinds": ["revision.synced"],
            **overrides,
        }
        response = self.client.post(
            "/api/v1/settings/webhooks", json=payload, headers=self.admin_headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_a_non_admin_is_refused_by_every_route(self) -> None:
        webhook_id = str(uuid.uuid4())
        for method, path, body in (
            ("get", "/api/v1/settings/webhooks", None),
            (
                "post",
                "/api/v1/settings/webhooks",
                {"url": "https://hooks.example.test", "eventKinds": ["revision.synced"]},
            ),
            ("patch", f"/api/v1/settings/webhooks/{webhook_id}", {"status": "disabled"}),
            ("delete", f"/api/v1/settings/webhooks/{webhook_id}", None),
            ("post", f"/api/v1/settings/webhooks/{webhook_id}/rotate-secret", None),
            ("post", f"/api/v1/settings/webhooks/{webhook_id}/test", None),
            ("get", f"/api/v1/settings/webhooks/{webhook_id}/deliveries", None),
        ):
            with self.subTest(method=method, path=path):
                response = getattr(self.client, method)(
                    path,
                    headers=self.headers,
                    **({"json": body} if body is not None else {}),
                )
                self.assertEqual(response.status_code, 403, response.text)

    def test_create_returns_the_secret_and_list_never_does(self) -> None:
        created = self.create(projectIds=None)

        self.assertTrue(created["secret"].startswith("whsec_"))
        self.assertEqual(created["eventKinds"], ["revision.synced"])
        self.assertEqual(created["status"], "active")

        listed = self.client.get(
            "/api/v1/settings/webhooks", headers=self.admin_headers
        ).json()
        self.assertEqual([row["id"] for row in listed], [created["id"]])
        self.assertNotIn("secret", listed[0])
        self.assertNotIn("secret_envelope", listed[0])
        self.assertNotIn(created["secret"], str(listed))

    def test_create_refuses_a_private_target(self) -> None:
        with patch.object(
            targets,
            "resolve_addresses",
            lambda _host, _port: [ipaddress.ip_address("127.0.0.1")],
        ):
            response = self.client.post(
                "/api/v1/settings/webhooks",
                json={
                    "url": "https://localhost/prism",
                    "eventKinds": ["revision.synced"],
                },
                headers=self.admin_headers,
            )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["code"], "webhook-target-refused")

    def test_create_refuses_an_unknown_event_kind(self) -> None:
        response = self.client.post(
            "/api/v1/settings/webhooks",
            json={"url": "https://hooks.example.test", "eventKinds": ["nope.happened"]},
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "unknown_event_kind")

    def test_patch_edits_filters_and_re_enables_a_failing_webhook(self) -> None:
        created = self.create()

        patched = self.client.patch(
            f"/api/v1/settings/webhooks/{created['id']}",
            json={
                "url": "https://hooks.example.test/changed",
                "eventKinds": ["bundle.built"],
                "projectIds": None,
                "status": "disabled",
            },
            headers=self.admin_headers,
        )
        self.assertEqual(patched.status_code, 200, patched.text)
        body = patched.json()
        self.assertEqual(body["url"], "https://hooks.example.test/changed")
        self.assertEqual(body["eventKinds"], ["bundle.built"])
        self.assertIsNone(body["projectIds"])
        self.assertEqual(body["status"], "disabled")

        null_url = self.client.patch(
            f"/api/v1/settings/webhooks/{created['id']}",
            json={"url": None},
            headers=self.admin_headers,
        )
        self.assertEqual(null_url.status_code, 422, null_url.text)
        self.assertEqual(null_url.json()["code"], "invalid_field")

    def test_rotate_secret_returns_a_new_secret_and_invalidates_the_old(self) -> None:
        created = self.create()

        rotated = self.client.post(
            f"/api/v1/settings/webhooks/{created['id']}/rotate-secret",
            headers=self.admin_headers,
        )
        self.assertEqual(rotated.status_code, 200, rotated.text)
        secret = rotated.json()["secret"]
        self.assertTrue(secret.startswith("whsec_"))
        self.assertNotEqual(secret, created["secret"])
        self.assertEqual(set(rotated.json()), {"secret"})

    def test_test_ping_reports_its_outcome_and_lands_in_deliveries(self) -> None:
        created = self.create()

        with patch.object(
            delivery, "http_sender", lambda url, body, headers: delivery.SendOutcome(204, True, None, 7)
        ):
            response = self.client.post(
                f"/api/v1/settings/webhooks/{created['id']}/test",
                headers=self.admin_headers,
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {"statusCode": 204, "ok": True, "error": None, "durationMs": 7},
        )

        rows = self.client.get(
            f"/api/v1/settings/webhooks/{created['id']}/deliveries",
            headers=self.admin_headers,
        ).json()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["eventId"])
        self.assertTrue(rows[0]["ok"])
        self.assertEqual(rows[0]["statusCode"], 204)

    def test_a_failed_ping_is_reported_not_raised(self) -> None:
        created = self.create()

        with patch.object(
            delivery,
            "http_sender",
            lambda url, body, headers: delivery.SendOutcome(None, False, "timeout", 9),
        ):
            response = self.client.post(
                f"/api/v1/settings/webhooks/{created['id']}/test",
                headers=self.admin_headers,
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {"statusCode": None, "ok": False, "error": "timeout", "durationMs": 9},
        )

    def test_delete_removes_the_registration(self) -> None:
        created = self.create()

        deleted = self.client.delete(
            f"/api/v1/settings/webhooks/{created['id']}", headers=self.admin_headers
        )
        self.assertEqual(deleted.status_code, 204, deleted.text)
        listed = self.client.get(
            "/api/v1/settings/webhooks", headers=self.admin_headers
        ).json()
        self.assertEqual(listed, [])

    def test_an_unknown_id_is_not_found(self) -> None:
        missing = str(uuid.uuid4())
        for method, path, body in (
            ("patch", f"/api/v1/settings/webhooks/{missing}", {"status": "disabled"}),
            ("delete", f"/api/v1/settings/webhooks/{missing}", None),
            ("post", f"/api/v1/settings/webhooks/{missing}/rotate-secret", None),
            ("post", f"/api/v1/settings/webhooks/{missing}/test", None),
            ("get", f"/api/v1/settings/webhooks/{missing}/deliveries", None),
        ):
            with self.subTest(method=method, path=path):
                response = getattr(self.client, method)(
                    path,
                    headers=self.admin_headers,
                    **({"json": body} if body is not None else {}),
                )
                self.assertEqual(response.status_code, 404, response.text)
                self.assertEqual(response.json()["code"], "webhook_not_found")


if __name__ == "__main__":
    unittest.main()
