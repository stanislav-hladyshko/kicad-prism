"""Webhook registrations: CRUD, one-time secrets and delivery history.

``WebhookStoreTests`` runs the service against a scratch schema migrated by the
production workspace ledger, so the migration and the SQL are the real ones.
"""

from __future__ import annotations

import ipaddress
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.core.errors import Conflict, Invalid, NotFound  # noqa: E402
from app.services import event_log  # noqa: E402
from app.services.trackers.secrets import SecretStoreLocked  # noqa: E402
from app.services.webhooks import store, targets  # noqa: E402
from app.services.workspace_schema_migrations import WORKSPACE_LEDGER  # noqa: E402
from support.postgres import (  # noqa: E402
    requires_scratch_postgres,
    scratch_schema,
    seed_project,
    seed_repository,
)

ROOT_KEY = "aa" * 32
PUBLIC = "93.184.216.34"


def resolves_public(_host: str, _port: int):
    return [ipaddress.ip_address(PUBLIC)]


@requires_scratch_postgres("webhook store tests")
class WebhookStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = scratch_schema(
            self, "webhooks_store", ledgers=(WORKSPACE_LEDGER,)
        )
        self.conn = self.scratch.conn
        for module in (store, event_log):
            patcher = patch.object(module, "connect", self.scratch.connection)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (
            ("TRACKER_CREDENTIAL_ROOT_KEY", SecretStr(ROOT_KEY)),
            ("TRACKER_CREDENTIAL_ROOT_KEY_ID", "v1"),
            ("TRACKER_CREDENTIAL_PREVIOUS_ROOT_KEY", SecretStr("")),
            ("TRACKER_CREDENTIAL_PREVIOUS_ROOT_KEY_ID", ""),
        ):
            patcher = patch.object(settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        resolver = patch.object(targets, "resolve_addresses", resolves_public)
        resolver.start()
        self.addCleanup(resolver.stop)

        suffix = uuid.uuid4().hex[:8]
        self.repository = f"repo-{suffix}"
        self.project = f"project-{suffix}"
        seed_repository(self.conn, self.repository)
        seed_project(self.conn, self.project, self.repository)
        self.conn.commit()

    def _create(self, **overrides):
        values = {
            "url": "https://hooks.example.test/prism",
            "event_kinds": ["revision.synced"],
            "project_ids": None,
            "created_by": "admin@example.test",
        }
        values.update(overrides)
        return store.create(**values)

    def test_create_returns_a_secret_once_and_starts_at_the_log_head(self) -> None:
        first = event_log.record("revision.synced", project_id=self.project)
        second = event_log.record("bundle.built", project_id=self.project)

        created, secret = self._create()

        self.assertTrue(secret.startswith("whsec_"))
        self.assertEqual(created["cursor"], second)
        self.assertGreater(created["cursor"], first)
        self.assertEqual(created["status"], "active")
        self.assertEqual(created["eventKinds"], ["revision.synced"])
        self.assertIsNone(created["projectIds"])
        self.assertNotIn("secret", created)
        self.assertNotIn("secret_envelope", created)

        row = store.get_row(created["id"])
        self.assertEqual(store.decrypt(row), secret)
        [listed] = store.list_all()
        self.assertEqual(listed, created)
        self.assertNotIn("secret", str(listed))
        self.assertNotIn("envelope", str(listed))

    def test_create_refuses_an_unknown_event_kind(self) -> None:
        with self.assertRaises(Invalid) as caught:
            self._create(event_kinds=["not.a.kind"])
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.code, "unknown_event_kind")

    def test_create_fails_closed_when_the_secret_store_is_locked(self) -> None:
        with patch.object(
            store, "encrypt_secret", side_effect=SecretStoreLocked("locked")
        ):
            with self.assertRaises(Conflict) as caught:
                self._create()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.code, "secret-store-locked")
        self.assertEqual(store.list_all(), [])

    def test_create_validates_the_target(self) -> None:
        from app.core.errors import BadRequest

        with patch.object(targets, "resolve_addresses", lambda _h, _p: [ipaddress.ip_address("10.0.0.1")]):
            with self.assertRaises(BadRequest) as caught:
                self._create()
        self.assertEqual(caught.exception.code, targets.REFUSED)

    def test_update_edits_filters_and_clears_project_ids_with_null(self) -> None:
        created, _secret = self._create(project_ids=[self.project])

        updated = store.update(
            created["id"],
            url="https://hooks.example.test/changed",
            event_kinds=["revision.synced", "bundle.built"],
            project_ids=None,
        )

        self.assertEqual(updated["url"], "https://hooks.example.test/changed")
        self.assertEqual(updated["eventKinds"], ["revision.synced", "bundle.built"])
        self.assertIsNone(updated["projectIds"])
        self.assertEqual(store.get_view(created["id"]), updated)
        with self.assertRaises(Invalid):
            store.update(created["id"], status="failing")

    def test_rotate_secret_returns_a_new_secret_once(self) -> None:
        created, old_secret = self._create()

        rotated, new_secret = store.rotate_secret(created["id"])

        self.assertNotEqual(new_secret, old_secret)
        self.assertTrue(new_secret.startswith("whsec_"))
        self.assertEqual(store.decrypt(store.get_row(created["id"])), new_secret)
        self.assertNotIn("secret", rotated)

    def test_re_enabling_a_failing_webhook_resets_its_retry_state(self) -> None:
        created, _secret = self._create()
        now = datetime.now(timezone.utc)
        store.record_failure(
            created["id"],
            attempts=4,
            failing_since=now - timedelta(hours=25),
            next_attempt_at=now,
            status="failing",
            status_code=500,
            error="HTTP 500",
            at=now,
        )
        self.assertEqual(store.get_view(created["id"])["status"], "failing")

        enabled = store.update(created["id"], status="active")

        self.assertEqual(enabled["status"], "active")
        self.assertEqual(enabled["attempts"], 0)
        self.assertIsNone(enabled["failingSince"])
        self.assertIsNone(enabled["nextAttemptAt"])

    def test_delete_removes_deliveries_with_the_registration(self) -> None:
        created, _secret = self._create()
        store.record_delivery(
            created["id"],
            event_id=1,
            status_code=200,
            ok=True,
            error=None,
            duration_ms=3,
        )

        store.delete(created["id"])

        self.assertEqual(store.list_all(), [])
        with self.assertRaises(NotFound):
            store.get_view(created["id"])
        with self.assertRaises(NotFound):
            store.delete(created["id"])
        with self.scratch.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM ws_webhook_deliveries"
            ).fetchone()["count"]
        self.assertEqual(count, 0)

    def test_deliveries_are_newest_first_and_pruned_by_age(self) -> None:
        created, _secret = self._create()
        with self.scratch.connect() as conn:
            conn.execute(
                """
                INSERT INTO ws_webhook_deliveries
                    (webhook_id, event_id, at, status_code, ok, error, duration_ms)
                VALUES (%s, 99, NOW() - INTERVAL '40 days', 200, TRUE, NULL, 1)
                """,
                (created["id"],),
            )
            conn.commit()
        for status in (200, 500):
            store.record_delivery(
                created["id"],
                event_id=status,
                status_code=status,
                ok=status == 200,
                error=None if status == 200 else "HTTP 500",
                duration_ms=1,
            )

        rows = store.list_deliveries(created["id"])
        self.assertEqual([row["statusCode"] for row in rows], [500, 200, 200])
        self.assertEqual(rows[-1]["eventId"], 99)

        self.assertGreaterEqual(store.prune_deliveries(30), 1)
        self.assertEqual(len(store.list_deliveries(created["id"])), 2)


if __name__ == "__main__":
    unittest.main()
