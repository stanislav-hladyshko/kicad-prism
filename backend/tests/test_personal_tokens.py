"""Personal access tokens: storage, bearer authentication, and management routes.

The service and bearer tests need PostgreSQL; the route tests drive the real app
with a signed-in session cookie and are skipped without a database or an HTTP
client for Starlette's TestClient.
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException  # noqa: E402

from app.core import policy  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.errors import BadRequest  # noqa: E402
from app.core.security import _resolve_bearer_user  # noqa: E402
from app.services import access_service, personal_tokens  # noqa: E402
from app.services.postgres_database import database  # noqa: E402

DATABASE_URL = os.environ.get("PRISM_DATABASE_URL", "").strip()

try:
    import starlette.testclient  # noqa: F401
    HAS_TEST_CLIENT = True
except RuntimeError:
    HAS_TEST_CLIENT = False

TEST_SECRET = "test-secret-at-least-32-characters-long-x"


def unique_email(label: str) -> str:
    return f"pat-{label}-{uuid.uuid4().hex[:10]}@example.com"


def use_user(email: str, role: str) -> dict[str, str]:
    access_service.upsert_user(email=email, name=email.split("@")[0])
    access_service.upsert_user_role(email, role, updated_by=email)
    user = access_service.get_user_by_email(email)
    assert user is not None
    return user


def bearer(token: str) -> dict:
    try:
        return _resolve_bearer_user(token)
    except HTTPException as exc:
        return {"status": exc.status_code, "detail": exc.detail}


@unittest.skipUnless(DATABASE_URL, "PRISM_DATABASE_URL is required for personal token tests")
class PersonalTokenServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        personal_tokens._last_touched.clear()

    def test_create_stores_only_the_hash_and_the_display_prefix(self) -> None:
        email = unique_email("hash")
        row, token = personal_tokens.create(email, "ci", ["api:read"], 30)

        self.assertTrue(token.startswith("prism_pat_"))
        self.assertEqual(len(token), len("prism_pat_") + 40)
        self.assertEqual(row["prefix"], token[:12])
        self.assertNotIn("token", row)
        self.assertNotIn("token_hash", row)

        with database.connection() as connection:
            connection.execute("SET search_path TO workspace, public")
            stored = connection.execute(
                "SELECT token_hash, prefix FROM ws_personal_access_tokens WHERE id = %s",
                (row["id"],),
            ).fetchone()
        self.assertEqual(stored["token_hash"], hashlib.sha256(token.encode()).hexdigest())
        self.assertNotEqual(stored["token_hash"], token)
        self.assertEqual(stored["prefix"], token[:12])

    def test_validate_refuses_revoked_and_expired_tokens(self) -> None:
        email = unique_email("validate")
        row, token = personal_tokens.create(email, "ci", ["api:read"], 30)
        self.assertEqual(personal_tokens.validate(token)["id"], row["id"])

        personal_tokens.revoke(row["id"], by_email=email, is_admin=False)
        self.assertIsNone(personal_tokens.validate(token))

        expired_row, expired_token = personal_tokens.create(email, "old", ["api:read"], 30)
        with database.connection() as connection:
            connection.execute("SET search_path TO workspace, public")
            connection.execute(
                "UPDATE ws_personal_access_tokens SET expires_at = NOW() - interval '1 second' WHERE id = %s",
                (expired_row["id"],),
            )
            connection.commit()
        self.assertIsNone(personal_tokens.validate(expired_token))

    def test_days_are_bounded_and_the_request_default_is_ninety(self) -> None:
        email = unique_email("days")
        with self.assertRaises(BadRequest):
            personal_tokens.create(email, "ci", ["api:read"], 0)
        with self.assertRaises(BadRequest):
            personal_tokens.create(email, "ci", ["api:read"], settings.PRISM_PAT_MAX_DAYS + 1)

        row, _token = personal_tokens.create(email, "ci", ["api:read"], 90)
        with database.connection() as connection:
            connection.execute("SET search_path TO workspace, public")
            span = connection.execute(
                """
                SELECT EXTRACT(DAY FROM expires_at - created_at)::int AS days
                FROM ws_personal_access_tokens WHERE id = %s
                """,
                (row["id"],),
            ).fetchone()
        self.assertEqual(span["days"], 90)

    def test_scope_validation_refuses_wildcards_and_provider_scopes(self) -> None:
        email = unique_email("scopes")
        for scopes in ([], ["*"], ["remote_symbols.read"], ["nonsense"]):
            with self.subTest(scopes=scopes), self.assertRaises(BadRequest):
                personal_tokens.create(email, "ci", scopes, 30)

        row, _token = personal_tokens.create(email, "ci", ["api:read", "design:write"], 30)
        self.assertEqual(row["scopes"], ["api:read", "design:write"])

    def test_name_is_bounded(self) -> None:
        email = unique_email("name")
        with self.assertRaises(BadRequest):
            personal_tokens.create(email, " ", ["api:read"], 30)
        with self.assertRaises(BadRequest):
            personal_tokens.create(email, "x" * 81, ["api:read"], 30)

    def test_touch_writes_at_most_once_a_minute_per_token(self) -> None:
        row = {"id": "pat_" + uuid.uuid4().hex[:16]}
        with patch.object(personal_tokens, "_touch_store") as store, patch.object(
            personal_tokens, "_now", return_value=1_000
        ):
            personal_tokens.touch(row)
            personal_tokens.touch(row)
            self.assertEqual(store.call_count, 1)

            personal_tokens._now.return_value = 1_060
            personal_tokens.touch(row)
            self.assertEqual(store.call_count, 2)


@unittest.skipUnless(DATABASE_URL, "PRISM_DATABASE_URL is required for personal token tests")
class PersonalTokenBearerTests(unittest.TestCase):
    def setUp(self) -> None:
        personal_tokens._last_touched.clear()

    def test_a_token_authenticates_as_a_pat_with_its_scopes(self) -> None:
        email = unique_email("bearer")
        user = use_user(email, "designer")
        row, token = personal_tokens.create(email, "ci", ["api:read", "design:write"], 30)

        resolved = bearer(token)

        self.assertEqual(resolved.auth_type, "pat")
        self.assertEqual(resolved.client_id, f"pat:{row['id']}")
        self.assertEqual(resolved.token_id, row["id"])
        self.assertEqual(resolved.user_id, user["user_id"])
        self.assertEqual(resolved.scopes, ["api:read", "design:write"])
        self.assertEqual(resolved.role, "designer")

    def test_a_role_change_takes_effect_on_the_next_request(self) -> None:
        email = unique_email("role")
        use_user(email, "designer")
        _row, token = personal_tokens.create(email, "ci", ["api:read", "design:write"], 30)
        self.assertEqual(bearer(token).role, "designer")

        access_service.upsert_user_role(email, "viewer", updated_by=email)
        self.assertEqual(bearer(token).role, "viewer")

    def test_a_token_whose_user_is_gone_is_refused(self) -> None:
        email = unique_email("ghost")
        _row, token = personal_tokens.create(email, "ci", ["api:read"], 30)

        refused = bearer(token)

        self.assertEqual(refused["status"], 401)
        self.assertEqual(refused["detail"], "Invalid bearer token")

    def test_an_invalid_token_is_refused(self) -> None:
        self.assertEqual(bearer("prism_pat_not-a-real-token")["status"], 401)

    def test_a_read_scope_does_not_buy_the_design_action(self) -> None:
        email = unique_email("design")
        use_user(email, "admin")
        _row, token = personal_tokens.create(email, "ci", ["api:read"], 30)

        user = bearer(token)
        with self.assertRaises(HTTPException) as caught:
            policy.authorize(user, "design")
        self.assertEqual(caught.exception.status_code, 403)


@unittest.skipUnless(DATABASE_URL, "PRISM_DATABASE_URL is required for personal token tests")
@unittest.skipUnless(HAS_TEST_CLIENT, "starlette.testclient needs httpx2 or httpx")
class PersonalTokenApiTests(unittest.TestCase):
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
        from app.services import session_store_service

        session_store_service.initialize_session_store()
        cls.main = main
        cls.session_store_service = session_store_service
        cls.TestClient = TestClient

    @classmethod
    def tearDownClass(cls) -> None:
        for patcher in getattr(cls, "_patchers", []):
            patcher.stop()

    def _client_for(self, email: str, role: str):
        from app.core.session import SESSION_COOKIE_NAME, create_session_token

        use_user(email, role)
        session_id, _ = self.session_store_service.create_session(
            email=email, name="Tester", picture=""
        )
        client = self.TestClient(self.main.app)
        client.cookies.set(SESSION_COOKIE_NAME, create_session_token(session_id))
        return client

    def _create_token(self, client, name: str = "ci", scopes: list[str] | None = None) -> dict:
        response = client.post(
            "/api/auth/tokens",
            json={"name": name, "scopes": scopes or ["api:read"], "expiresInDays": 30},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_a_session_creates_a_token_and_the_list_never_reveals_it(self) -> None:
        email = unique_email("api-create")
        client = self._client_for(email, "designer")
        created = self._create_token(client)

        self.assertTrue(created["token"].startswith("prism_pat_"))
        self.assertEqual(created["prefix"], created["token"][:12])
        self.assertEqual(created["user_email"], email)

        listed = client.get("/api/auth/tokens")
        self.assertEqual(listed.status_code, 200)
        rows = listed.json()
        self.assertEqual(len(rows), 1)
        for row in rows:
            self.assertNotIn("token", row)
            self.assertNotIn("token_hash", row)
        self.assertEqual(rows[0]["id"], created["id"])

    def test_the_request_defaults_to_ninety_days(self) -> None:
        from datetime import datetime

        email = unique_email("api-default")
        client = self._client_for(email, "designer")
        created = client.post(
            "/api/auth/tokens", json={"name": "ci", "scopes": ["api:read"]}
        ).json()
        span = datetime.fromisoformat(created["expires_at"]) - datetime.fromisoformat(
            created["created_at"]
        )
        self.assertEqual(span.days, 90)

    def test_a_pat_cannot_mint_another_token(self) -> None:
        email = unique_email("api-pat-create")
        client = self._client_for(email, "designer")
        token = self._create_token(client)["token"]

        response = self.TestClient(self.main.app).post(
            "/api/auth/tokens",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "nested", "scopes": ["api:read"], "expiresInDays": 30},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "session_required")

    def test_creation_is_refused_when_auth_is_disabled(self) -> None:
        client = self.TestClient(self.main.app)
        with patch.object(settings, "AUTH_ENABLED_OVERRIDE", False):
            response = client.post(
                "/api/auth/tokens",
                json={"name": "ci", "scopes": ["api:read"], "expiresInDays": 30},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "auth_disabled")

    def test_revoking_someone_elses_token_is_404_and_an_admin_may(self) -> None:
        _owner_email = unique_email("api-owner")
        owner = self._client_for(_owner_email, "designer")
        token_id = self._create_token(owner)["id"]

        intruder = self._client_for(unique_email("api-intruder"), "designer")
        refused = intruder.delete(f"/api/auth/tokens/{token_id}")
        self.assertEqual(refused.status_code, 404)

        admin = self._client_for(unique_email("api-admin"), "admin")
        revoked = admin.delete(f"/api/auth/tokens/{token_id}")
        self.assertEqual(revoked.status_code, 204)

    def test_listing_everyones_tokens_is_admin_only(self) -> None:
        viewer = self._client_for(unique_email("api-viewer"), "viewer")
        self.assertEqual(viewer.get("/api/auth/tokens/all").status_code, 403)

        admin = self._client_for(unique_email("api-all-admin"), "admin")
        admin_token = self._create_token(admin)
        everyone = admin.get("/api/auth/tokens/all")
        self.assertEqual(everyone.status_code, 200)
        self.assertIn(admin_token["id"], {row["id"] for row in everyone.json()})
        for row in everyone.json():
            self.assertNotIn("token", row)
            self.assertNotIn("token_hash", row)

    def test_a_pat_clones_a_visible_project_over_basic_auth(self) -> None:
        """Git has no bearer scheme, so a PAT arrives as the Basic password."""
        from app.services import git_host_service

        email = unique_email("api-git")
        client = self._client_for(email, "viewer")
        token = self._create_token(client)["token"]
        project_id = "prj_pat" + uuid.uuid4().hex[:8]

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with patch.object(settings, "KICAD_PROJECTS_ROOT", temporary.name), patch(
            "app.api._helpers.workspace.get_project_for_role",
            return_value={"id": project_id, "name": "Board"},
        ):
            git_host_service.create(project_id)
            encoded = base64.b64encode(f"anything:{token}".encode()).decode()
            response = self.TestClient(self.main.app).get(
                f"/git/{project_id}.git/info/refs",
                params={"service": "git-upload-pack"},
                headers={"Authorization": f"Basic {encoded}"},
            )

        self.assertEqual(response.status_code, 200, response.text)


if __name__ == "__main__":
    unittest.main()
