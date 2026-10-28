"""One webhook delivery pass: filters, retries, the 24-hour stop, re-enable.

The send function is injected, so the pass is exercised without a network. The
database is a scratch schema migrated by the production workspace ledger.
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
from app.services import event_log  # noqa: E402
from app.services.webhooks import delivery, store, targets  # noqa: E402
from app.services.workspace_schema_migrations import WORKSPACE_LEDGER  # noqa: E402
from support.postgres import (  # noqa: E402
    requires_scratch_postgres,
    scratch_schema,
    seed_project,
    seed_repository,
)

ROOT_KEY = "bb" * 32
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def resolves_public(_host: str, _port: int):
    return [ipaddress.ip_address("93.184.216.34")]


class FakeSender:
    """Records each call and returns the queued outcomes (200 unless set)."""

    def __init__(self, outcomes: list[delivery.SendOutcome] | None = None) -> None:
        self.calls: list[dict] = []
        self.outcomes = list(outcomes or [])

    def __call__(self, url: str, body: bytes, headers) -> delivery.SendOutcome:
        self.calls.append({"url": url, "body": body, "headers": dict(headers)})
        if self.outcomes:
            return self.outcomes.pop(0)
        return delivery.SendOutcome(200, True, None, 1)


@requires_scratch_postgres("webhook delivery tests")
class WebhookDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = scratch_schema(
            self, "webhooks_delivery", ledgers=(WORKSPACE_LEDGER,)
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
        self.other_project = f"project-other-{suffix}"
        seed_repository(self.conn, self.repository)
        seed_project(self.conn, self.project, self.repository)
        seed_project(self.conn, self.other_project, self.repository)
        self.conn.commit()

    def _create(self, **overrides):
        values = {
            "url": "https://hooks.example.test/prism",
            "event_kinds": ["revision.synced"],
            "project_ids": None,
            "created_by": "admin@example.test",
        }
        values.update(overrides)
        created, secret = store.create(**values)
        return created, secret

    def _view(self, webhook_id: str) -> dict:
        return store.get_view(webhook_id)

    def _set_retry_state(self, webhook_id: str, failing_since: datetime, attempts: int) -> None:
        with self.scratch.connect() as conn:
            conn.execute(
                """
                UPDATE ws_webhooks
                SET failing_since = %s,
                    attempts = %s,
                    next_attempt_at = %s,
                    status = 'active'
                WHERE id = %s
                """,
                (failing_since, attempts, NOW, webhook_id),
            )
            conn.commit()

    def test_a_pass_does_nothing_while_another_worker_holds_the_lock(self) -> None:
        self._create()
        event_log.record("revision.synced", project_id=self.project, payload={})
        with self.scratch.connect() as holder:
            holder.execute("SELECT pg_advisory_lock(%s)", (delivery.PASS_LOCK_KEY,))
            sender = FakeSender()
            stats = delivery.run_pass(sender=sender, now=NOW)
            holder.execute("SELECT pg_advisory_unlock(%s)", (delivery.PASS_LOCK_KEY,))
        self.assertEqual(stats["webhooks"], 0)
        self.assertEqual(sender.calls, [])
        after = FakeSender()
        delivery.run_pass(sender=after, now=NOW)
        self.assertEqual(len(after.calls), 1)

    def test_the_http_sender_refuses_a_target_that_now_resolves_inward(self) -> None:
        with patch.object(targets, "resolve_addresses", lambda _h, _p: [ipaddress.ip_address("10.0.0.5")]):
            outcome = delivery.http_sender("https://hooks.example.test/prism", b"{}", {})
        self.assertFalse(outcome.ok)
        self.assertIsNone(outcome.status_code)
        self.assertIn("public address", outcome.error or "")

    def test_the_http_sender_connects_to_the_checked_address(self) -> None:
        seen = {}

        class Pool:
            def __init__(self, address, **kwargs):
                seen["address"] = address
                seen["server_hostname"] = kwargs.get("server_hostname")

            def request(self, method, path, **kwargs):
                seen["host_header"] = kwargs["headers"]["Host"]
                seen["path"] = path

                class Response:
                    status = 204

                return Response()

            def close(self):
                pass

        with patch.object(delivery.urllib3, "HTTPSConnectionPool", Pool):
            outcome = delivery.http_sender("https://hooks.example.test/prism?a=1", b"{}", {})
        self.assertTrue(outcome.ok)
        self.assertEqual(seen["address"], "93.184.216.34")
        self.assertEqual(seen["server_hostname"], "hooks.example.test")
        self.assertEqual(seen["host_header"], "hooks.example.test")
        self.assertEqual(seen["path"], "/prism?a=1")

    def test_a_pass_delivers_matching_events_in_order_and_advances_the_cursor(self) -> None:
        created, _secret = self._create()
        first = event_log.record("revision.synced", project_id=self.project, payload={"n": 1})
        second = event_log.record("revision.synced", project_id=self.project, payload={"n": 2})

        sender = FakeSender()
        stats = delivery.run_pass(sender=sender, now=NOW)

        self.assertEqual(stats["webhooks"], 1)
        self.assertEqual(stats["delivered"], 2)
        self.assertEqual(len(sender.calls), 2)
        bodies = [call["body"] for call in sender.calls]
        self.assertIn(b'"id":%d' % first, bodies[0])
        self.assertIn(b'"id":%d' % second, bodies[1])
        self.assertEqual(sender.calls[0]["headers"]["webhook-id"], str(first))
        self.assertEqual(sender.calls[1]["headers"]["webhook-id"], str(second))

        view = self._view(created["id"])
        self.assertEqual(view["cursor"], second)
        self.assertEqual(view["attempts"], 0)
        self.assertIsNone(view["failingSince"])
        self.assertIsNone(view["nextAttemptAt"])
        self.assertEqual(view["lastStatus"], 200)
        rows = store.list_deliveries(created["id"])
        self.assertEqual([row["eventId"] for row in rows], [second, first])
        self.assertTrue(all(row["ok"] for row in rows))

    def test_filtered_events_advance_the_cursor_without_sending(self) -> None:
        matching, _secret = self._create()
        scoped, _secret = self._create(project_ids=[self.project])
        skipped_kind = event_log.record("job.finished", project_id=self.project)
        wanted = event_log.record("revision.synced", project_id=self.project)
        other = event_log.record("revision.synced", project_id=self.other_project)

        sender = FakeSender()
        delivery.run_pass(sender=sender, now=NOW)

        self.assertEqual(len(sender.calls), 3)
        self.assertEqual(self._view(matching["id"])["cursor"], other)
        self.assertEqual(self._view(scoped["id"])["cursor"], other)
        self.assertGreater(skipped_kind, 0)
        # The global webhook sees both project events; the scoped one only its own.
        self.assertEqual(
            sorted(call["headers"]["webhook-id"] for call in sender.calls),
            sorted([str(wanted), str(wanted), str(other)]),
        )

    def test_a_failure_stops_the_pass_and_doubles_the_backoff(self) -> None:
        created, _secret = self._create()
        event_log.record("revision.synced", project_id=self.project)
        event_log.record("revision.synced", project_id=self.project)

        failing = FakeSender([delivery.SendOutcome(500, False, "HTTP 500", 4)])
        stats = delivery.run_pass(sender=failing, now=NOW)

        self.assertEqual(stats["failed"], 1)
        self.assertEqual(len(failing.calls), 1)
        view = self._view(created["id"])
        self.assertEqual(view["attempts"], 1)
        self.assertEqual(view["status"], "active")
        self.assertEqual(view["failingSince"], NOW)
        self.assertEqual(view["nextAttemptAt"], NOW + timedelta(seconds=30))
        self.assertEqual(view["lastError"], "HTTP 500")
        self.assertEqual(created["cursor"], view["cursor"])
        [row] = store.list_deliveries(created["id"])
        self.assertFalse(row["ok"])

        not_due = FakeSender()
        delivery.run_pass(sender=not_due, now=NOW + timedelta(seconds=10))
        self.assertEqual(not_due.calls, [])

        due = FakeSender([delivery.SendOutcome(500, False, "HTTP 500", 4)])
        delivery.run_pass(sender=due, now=NOW + timedelta(seconds=30))
        self.assertEqual(len(due.calls), 1)
        view = self._view(created["id"])
        self.assertEqual(view["attempts"], 2)
        self.assertEqual(
            view["nextAttemptAt"], NOW + timedelta(seconds=30) + timedelta(seconds=60)
        )

    def test_twenty_four_hours_of_failure_is_failing_until_re_enabled(self) -> None:
        created, _secret = self._create()
        event_log.record("revision.synced", project_id=self.project)
        self._set_retry_state(created["id"], NOW - timedelta(hours=25), attempts=3)

        delivery.run_pass(
            sender=FakeSender([delivery.SendOutcome(None, False, "timeout", 1)]), now=NOW
        )

        view = self._view(created["id"])
        self.assertEqual(view["status"], "failing")
        self.assertEqual(view["attempts"], 4)
        self.assertEqual(store.due(NOW + timedelta(hours=1)), [])

        quiet = FakeSender()
        delivery.run_pass(sender=quiet, now=NOW + timedelta(days=1))
        self.assertEqual(quiet.calls, [])

        store.update(created["id"], status="active")
        view = self._view(created["id"])
        self.assertEqual(view["status"], "active")
        self.assertEqual(view["attempts"], 0)
        self.assertIsNone(view["failingSince"])
        self.assertIsNone(view["nextAttemptAt"])
        self.assertEqual(len(store.due(NOW + timedelta(days=1))), 1)

    def test_a_successful_retry_then_a_failure_starts_backoff_from_zero(self) -> None:
        created, _secret = self._create()
        event_log.record("revision.synced", project_id=self.project)
        event_log.record("revision.synced", project_id=self.project)
        self._set_retry_state(created["id"], NOW - timedelta(minutes=5), attempts=2)

        sender = FakeSender(
            [
                delivery.SendOutcome(200, True, None, 1),
                delivery.SendOutcome(500, False, "HTTP 500", 1),
            ]
        )
        delivery.run_pass(sender=sender, now=NOW)

        view = self._view(created["id"])
        self.assertEqual(view["attempts"], 1)
        self.assertEqual(view["nextAttemptAt"], NOW + timedelta(seconds=30))
        self.assertEqual(view["failingSince"], NOW)

    def test_delivery_rows_older_than_thirty_days_are_pruned(self) -> None:
        created, _secret = self._create()
        store.update(created["id"], status="disabled")
        with self.scratch.connect() as conn:
            conn.execute(
                """
                INSERT INTO ws_webhook_deliveries
                    (webhook_id, event_id, at, status_code, ok, error, duration_ms)
                VALUES (%s, 1, NOW() - INTERVAL '31 days', 200, TRUE, NULL, 1),
                       (%s, 2, NOW(), 200, TRUE, NULL, 1)
                """,
                (created["id"], created["id"]),
            )
            conn.commit()

        stats = delivery.run_pass(sender=FakeSender(), now=NOW)

        self.assertEqual(stats["pruned"], 1)
        self.assertEqual(len(store.list_deliveries(created["id"])), 1)

    def test_a_test_ping_signs_a_ping_and_never_moves_the_cursor(self) -> None:
        created, _secret = self._create()

        result = delivery.send_ping(
            created["id"], sender=FakeSender(), now=NOW
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(self._view(created["id"])["cursor"], created["cursor"])
        [row] = store.list_deliveries(created["id"])
        self.assertIsNone(row["eventId"])
        self.assertTrue(row["ok"])

    def test_a_ping_uses_the_ping_event_type(self) -> None:
        created, _secret = self._create()
        sender = FakeSender()

        delivery.send_ping(created["id"], sender=sender, now=NOW)

        [call] = sender.calls
        self.assertIn(b'"type":"ping"', call["body"])
        self.assertTrue(call["headers"]["webhook-id"])


if __name__ == "__main__":
    unittest.main()
