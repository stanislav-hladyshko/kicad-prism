"""MCP-02b: comments record the auth type, client and token that acted.

An MCP client acts for a signed-in user (ADR scripting-and-mcp §3.5): the
person owns the write, and the actor kind, client and token say which door it
came through. Sessions and guests keep those columns NULL.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.security import AuthenticatedUser, guest_user  # noqa: E402
from app.services import comment_permissions as cp  # noqa: E402
from app.services import comments_schema_migrations  # noqa: E402
from app.services.comments_schema_migrations import COMMENTS_LEDGER, _m016_actor_columns  # noqa: E402
from support.postgres import comments_store_on, requires_scratch_postgres, scratch_schema  # noqa: E402


def session(user_id="u_1", *, role="designer", name="Priya"):
    return AuthenticatedUser(
        email=f"{user_id}@example.com",
        name=name,
        role=role,
        session_id="sid-1",
        user_id=user_id,
    )


def mcp_client(user_id="u_1", *, role="designer", name="Priya", client_id="mcp-client-1", token_id="tok-1"):
    return AuthenticatedUser(
        email=f"{user_id}@example.com",
        name=name,
        role=role,
        auth_type="mcp",
        client_id=client_id,
        token_id=token_id,
        scopes=["comments:write"],
        user_id=user_id,
    )


class ActorIdentityTests(unittest.TestCase):
    def test_mcp_caller_resolves_like_the_user_it_acts_for(self) -> None:
        mcp = cp.resolve_actor(mcp_client("u_7f2a"))
        signed_in = cp.resolve_actor(session("u_7f2a"))
        self.assertEqual(mcp.actor_id, signed_in.actor_id)
        self.assertEqual(mcp.actor_id, "u_7f2a")
        self.assertEqual((mcp.actor_kind, signed_in.actor_kind), ("agent", "user"))

    def test_client_and_token_flow_into_actor_identity(self) -> None:
        for user, expected_kind in (
            (mcp_client(client_id="mcp-1", token_id="tok-a"), "agent"),
            (session(), "user"),
            (
                AuthenticatedUser(
                    email="svc@service.local", name="CI", role="designer",
                    auth_type="service_client", client_id="svc-ci", token_id="tok-b",
                ),
                "service",
            ),
            (
                AuthenticatedUser(
                    email="ext@external.oauth", name="ext", role="viewer",
                    auth_type="external_service", client_id="ext-1", token_id="tok-c",
                ),
                "service",
            ),
        ):
            with self.subTest(kind=expected_kind):
                actor = cp.resolve_actor(user)
                self.assertEqual((actor.actor_kind, actor.client_id, actor.token_id),
                                 (expected_kind, user.client_id, user.token_id))

        guest = cp.resolve_actor(guest_user())
        self.assertEqual((guest.client_id, guest.token_id), ("", ""))


@requires_scratch_postgres("comment actor column tests")
class ActorColumnStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = scratch_schema(self, "comments_actor", create=False)
        self.store = comments_store_on(self.scratch)
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.project_id = "prj_actor"
        self.path = self.tempdir.name
        self.store.initialize()

    def _revision_row(self, target_kind: str, target_id: str):
        with self.store._connect() as conn:
            return conn.execute(
                """
                SELECT editor_auth_type, editor_client_id, editor_token_id
                FROM comment_revisions
                WHERE project_id = %s AND target_kind = %s AND target_id = %s
                ORDER BY revision DESC LIMIT 1
                """,
                (self.project_id, target_kind, target_id),
            ).fetchone()

    def test_mcp_root_and_reply_carry_the_client_and_revision_door(self) -> None:
        actor = cp.resolve_actor(mcp_client("u_mcp", client_id="mcp-9", token_id="tok-9"))
        root = self.store.create_comment(
            self.project_id, self.path, "PCB", {"x": 1, "y": 2}, "root", actor=actor,
        )
        self.assertEqual((root["authorKind"], root["authorClientId"]), ("agent", "mcp-9"))
        self.assertEqual((root["authorUserId"], root["author"]), ("u_mcp", "Priya"))

        _, reply = self.store.add_reply(self.project_id, self.path, root["id"], "reply", actor=actor)
        self.assertEqual((reply["authorKind"], reply["authorClientId"]), ("agent", "mcp-9"))

        # The codec's read path carries the same fields after a reload.
        reloaded = self.store.get_comment(self.project_id, self.path, root["id"])
        self.assertEqual(reloaded["authorKind"], "agent")
        self.assertEqual(reloaded["authorClientId"], "mcp-9")
        self.assertEqual(reloaded["replies"][0]["authorKind"], "agent")
        self.assertEqual(reloaded["replies"][0]["authorClientId"], "mcp-9")

        for kind, target_id in (("root", root["id"]), ("reply", reply["id"])):
            row = self._revision_row(kind, target_id)
            self.assertEqual((row["editor_auth_type"], row["editor_client_id"], row["editor_token_id"]),
                             ("mcp", "mcp-9", "tok-9"))

        history = self.store.get_history(self.project_id, "root", root["id"])
        self.assertEqual((history[0]["editorAuthType"], history[0]["editorClientId"]), ("mcp", "mcp-9"))
        self.assertNotIn("editorTokenId", history[0])

    def test_session_actor_stores_null_client_and_token(self) -> None:
        actor = cp.resolve_actor(session("u_session"))
        root = self.store.create_comment(
            self.project_id, self.path, "PCB", {"x": 1, "y": 2}, "root", actor=actor,
        )
        self.assertEqual((root["authorKind"], root["authorClientId"]), ("user", None))

        _, reply = self.store.add_reply(self.project_id, self.path, root["id"], "reply", actor=actor)
        self.assertEqual((reply["authorKind"], reply["authorClientId"]), ("user", None))

        with self.store._connect() as conn:
            columns = conn.execute(
                """
                SELECT author_client_id FROM comments WHERE id = %s
                UNION ALL
                SELECT author_client_id FROM comment_replies WHERE id = %s
                """,
                (root["id"], reply["id"]),
            ).fetchall()
        self.assertEqual([row["author_client_id"] for row in columns], [None, None])

        for kind, target_id in (("root", root["id"]), ("reply", reply["id"])):
            row = self._revision_row(kind, target_id)
            self.assertEqual((row["editor_auth_type"], row["editor_client_id"], row["editor_token_id"]),
                             ("session", None, None))


@requires_scratch_postgres("comment actor migration test")
class MigrationTests(unittest.TestCase):
    def test_migration_16_is_idempotent(self) -> None:
        scratch = scratch_schema(self, "comments_actor_m16", ledgers=(COMMENTS_LEDGER,))
        _m016_actor_columns(scratch.conn)
        _m016_actor_columns(scratch.conn)
        scratch.conn.commit()

        columns = {
            (row["table_name"], row["column_name"])
            for row in scratch.conn.execute(
                """
                SELECT table_name, column_name FROM information_schema.columns
                WHERE table_schema = %s
                """,
                (scratch.name,),
            ).fetchall()
        }
        self.assertGreaterEqual(
            columns,
            {
                ("comment_revisions", "editor_auth_type"),
                ("comment_revisions", "editor_client_id"),
                ("comment_revisions", "editor_token_id"),
                ("comments", "author_client_id"),
                ("comment_replies", "author_client_id"),
            },
        )

        versions = [version for version, _, _ in comments_schema_migrations.MIGRATIONS]
        self.assertIn(16, versions)
