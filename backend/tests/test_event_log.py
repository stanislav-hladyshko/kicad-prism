"""The append-only event log and its polling route.

``EventLogPostgresTests`` runs the service against a scratch schema migrated by
the production workspace ledger. ``EventApiTests`` drives the real app with a
signed-in session, so it needs the application database.
"""

from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import event_log  # noqa: E402
from app.services.workspace_schema_migrations import WORKSPACE_LEDGER  # noqa: E402
from support.postgres import (  # noqa: E402
    requires_scratch_postgres,
    scratch_schema,
    seed_folder,
    seed_project,
    seed_repository,
)

DATABASE_URL = os.environ.get("PRISM_DATABASE_URL", "").strip()

try:
    import starlette.testclient  # noqa: F401

    HAS_TEST_CLIENT = True
except RuntimeError:
    HAS_TEST_CLIENT = False

TEST_SECRET = "test-secret-at-least-32-characters-long-x"


@requires_scratch_postgres("event log tests")
class EventLogPostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = scratch_schema(self, "event_log", ledgers=(WORKSPACE_LEDGER,))
        self.conn = self.scratch.conn
        patcher = patch.object(event_log, "connect", self.scratch.connection)
        patcher.start()
        self.addCleanup(patcher.stop)
        suffix = uuid.uuid4().hex[:8]
        self.repository = f"repo-{suffix}"
        self.project = f"project-{suffix}"
        seed_repository(self.conn, self.repository)
        seed_project(self.conn, self.project, self.repository)
        self.conn.commit()

    def _events(self, after: int = 0, *, role: str = "viewer", is_admin: bool = False, topics=None, limit=200):
        return event_log.list_after(
            after, role=role, is_admin=is_admin, topics=topics, limit=limit
        )

    def _row(self, event_id: int):
        with self.scratch.connect() as conn:
            return conn.execute("SELECT * FROM ws_events WHERE id = %s", (event_id,)).fetchone()

    def test_a_later_writer_waits_so_ids_become_visible_in_order(self) -> None:
        import threading

        holder = self.scratch.connect()
        first = holder.__enter__()
        try:
            first_id = event_log.record("job.finished", project_id=self.project, conn=first)
            second: dict[str, int] = {}
            writer = threading.Thread(
                target=lambda: second.update(id=event_log.record("job.finished", project_id=self.project))
            )
            writer.start()
            writer.join(timeout=1.0)
            # The second writer is blocked on the head row; nothing past the cursor is visible.
            self.assertTrue(writer.is_alive())
            self.assertEqual(self._events()[0], [])
            first.commit()
        finally:
            holder.__exit__(None, None, None)
        writer.join(timeout=10)
        self.assertFalse(writer.is_alive())
        self.assertEqual(second["id"], first_id + 1)
        self.assertEqual([event["id"] for event in self._events()[0]], [first_id, first_id + 1])

    def test_an_unknown_kind_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            event_log.record("nope.happened", project_id=self.project)

    def test_the_topic_defaults_to_the_project(self) -> None:
        event_id = event_log.record("job.finished", project_id=self.project)
        self.assertEqual(self._row(event_id)["topic"], f"project:{self.project}")

    def test_an_event_without_a_project_needs_a_topic(self) -> None:
        with self.assertRaises(ValueError):
            event_log.record("job.finished", project_id=None)
        event_id = event_log.record("job.finished", project_id=None, topic="job:abc")
        self.assertEqual(self._row(event_id)["topic"], "job:abc")

    def test_the_cursor_returns_events_oldest_first_and_ends_where_it_started(self) -> None:
        ids = [event_log.record("job.progress", project_id=self.project) for _ in range(3)]

        events, cursor = self._events()
        self.assertEqual([event["id"] for event in events], ids)
        self.assertEqual(cursor, ids[-1])

        rest, end = self._events(cursor)
        self.assertEqual(rest, [])
        self.assertEqual(end, cursor)

    def test_the_limit_bounds_one_page_and_moves_the_cursor(self) -> None:
        ids = [event_log.record("job.progress", project_id=self.project) for _ in range(3)]

        events, cursor = self._events(limit=2)
        self.assertEqual([event["id"] for event in events], ids[:2])
        self.assertEqual(cursor, ids[1])

        with self.assertRaises(ValueError):
            self._events(limit=0)
        with self.assertRaises(ValueError):
            self._events(limit=501)

    def test_topics_filter_to_exact_matches(self) -> None:
        wanted = event_log.record("bundle.built", project_id=self.project, topic="bundle:a")
        event_log.record("bundle.built", project_id=self.project, topic="bundle:b")

        events, _cursor = self._events(topics=["bundle:a"])
        self.assertEqual([event["id"] for event in events], [wanted])

    def test_a_hidden_folder_hides_its_events_from_a_viewer_but_not_an_admin(self) -> None:
        folder = f"folder-{uuid.uuid4().hex[:8]}"
        seed_folder(
            self.conn,
            folder,
            visibility_mode="roles",
            allowed_roles=["designer", "admin"],
        )
        hidden_project = f"project-{uuid.uuid4().hex[:8]}"
        seed_project(self.conn, hidden_project, self.repository, folder_id=folder)
        self.conn.commit()
        event_id = event_log.record("revision.synced", project_id=hidden_project)

        self.assertEqual(self._events(role="viewer"), ([], 0))
        events, _cursor = self._events(role="admin", is_admin=True)
        self.assertEqual([event["id"] for event in events], [event_id])

    def test_a_project_less_event_is_admin_only(self) -> None:
        event_id = event_log.record("job.finished", project_id=None, topic="job:global")

        self.assertEqual(self._events(role="viewer"), ([], 0))
        events, _cursor = self._events(role="admin", is_admin=True)
        self.assertEqual([event["id"] for event in events], [event_id])

    def test_the_sweep_removes_only_rows_past_retention(self) -> None:
        old = event_log.record("job.finished", project_id=self.project)
        fresh = event_log.record("job.finished", project_id=self.project)
        with self.scratch.connect() as conn:
            conn.execute(
                "UPDATE ws_events SET created_at = NOW() - INTERVAL '40 days' WHERE id = %s",
                (old,),
            )
            conn.commit()

        self.assertEqual(event_log.sweep_older_than(30), 1)
        self.assertIsNone(self._row(old))
        self.assertIsNotNone(self._row(fresh))

    def test_a_record_on_a_rolled_back_transaction_leaves_no_row(self) -> None:
        with self.scratch.connect() as conn:
            with self.assertRaises(RuntimeError), conn.transaction():
                event_log.record("comment.changed", project_id=self.project, conn=conn)
                raise RuntimeError("abort the mutation")

            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS count FROM ws_events").fetchone()["count"], 0
            )

    def test_record_quietly_swallows_and_logs_a_database_error(self) -> None:
        with patch.object(event_log, "record", side_effect=RuntimeError("database is down")):
            with self.assertLogs(event_log.logger, level="WARNING") as logged:
                self.assertIsNone(
                    event_log.record_quietly("bundle.built", project_id=self.project)
                )
        self.assertIn("bundle.built", "\n".join(logged.output))
        self.assertEqual(self._events()[0], [])


@unittest.skipUnless(DATABASE_URL, "PRISM_DATABASE_URL is required for the events route test")
@unittest.skipUnless(HAS_TEST_CLIENT, "starlette.testclient needs httpx2 or httpx")
class EventApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from starlette.testclient import TestClient

        from app.core.config import settings

        # Auth on for the whole class: the route needs a signed-in session.
        cls._patchers = [
            patch.object(settings, "AUTH_ENABLED_OVERRIDE", True),
            patch.object(settings, "PASSWORD_AUTH_ENABLED", True),
            patch.object(settings, "SESSION_SECRET", TEST_SECRET),
        ]
        for patcher in cls._patchers:
            patcher.start()

        import app.main as main
        from app.services import access_service, session_store_service

        session_store_service.initialize_session_store()
        cls.main = main
        cls.access_service = access_service
        cls.session_store_service = session_store_service
        cls.TestClient = TestClient

    @classmethod
    def tearDownClass(cls) -> None:
        for patcher in getattr(cls, "_patchers", []):
            patcher.stop()

    def _admin_client(self):
        from app.core.session import SESSION_COOKIE_NAME, create_session_token

        email = f"events-admin-{uuid.uuid4().hex[:8]}@example.com"
        self.access_service.upsert_user(email=email, name="events")
        self.access_service.upsert_user_role(email, "admin", updated_by=email)
        session_id, _ = self.session_store_service.create_session(email=email, name="Events", picture="")
        client = self.TestClient(self.main.app)
        client.cookies.set(SESSION_COOKIE_NAME, create_session_token(session_id))
        return client

    def _delete_event(self, event_id: int) -> None:
        from app.services.postgres_database import database

        with database.connection() as conn:
            conn.execute("SET search_path TO workspace, public")
            conn.execute("DELETE FROM ws_events WHERE id = %s", (event_id,))

    def test_the_route_returns_a_typed_page(self) -> None:
        topic = f"test:route:{uuid.uuid4().hex[:8]}"
        event_id = event_log.record(
            "job.finished", project_id=None, topic=topic, payload={"jobId": "job-1"}
        )
        self.addCleanup(self._delete_event, event_id)

        response = self._admin_client().get(
            "/api/v1/events", params={"after": 0, "topic": topic}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"events", "next"})
        self.assertEqual(body["next"], event_id)
        self.assertEqual(
            set(body["events"][0]),
            {"id", "kind", "projectId", "topic", "createdAt", "payload"},
        )
        self.assertEqual(body["events"][0]["kind"], "job.finished")
        self.assertEqual(body["events"][0]["payload"], {"jobId": "job-1"})
        self.assertIsNone(body["events"][0]["projectId"])
        self.assertTrue(body["events"][0]["createdAt"].endswith("Z"))

    def test_a_zero_limit_is_a_validation_error(self) -> None:
        response = self._admin_client().get("/api/v1/events", params={"limit": 0})
        self.assertEqual(response.status_code, 422)



class CommentChangeQueueTests(unittest.TestCase):
    """comment.changed is queued per writer, never on the pooled connection."""

    def test_a_change_outside_a_writer_scope_is_not_queued(self) -> None:
        from app.services import comment_live_events

        comment_live_events._queue_event(project_id="p", comment_id="c1", change_kind="created")
        with comment_live_events.queued_events() as queued:
            comment_live_events._queue_event(project_id="p", comment_id="c2", change_kind="edited")
        self.assertEqual(
            queued, [{"projectId": "p", "commentId": "c2", "changeKind": "edited"}]
        )

    def test_nested_writers_keep_their_own_changes(self) -> None:
        from app.services import comment_live_events

        with comment_live_events.queued_events() as outer:
            with comment_live_events.queued_events() as inner:
                comment_live_events._queue_event(project_id="p", comment_id="c", change_kind="resolved")
            comment_live_events._queue_event(project_id="p", comment_id="d", change_kind="created")
        self.assertEqual([change["commentId"] for change in inner], ["c"])
        self.assertEqual([change["commentId"] for change in outer], ["d"])

if __name__ == "__main__":
    unittest.main()
