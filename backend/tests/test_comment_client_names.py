"""Agent comments name their client ("Claude Code"), not its metadata URL."""

from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import comment_client_names, workspace_service  # noqa: E402
from app.services.postgres_database import database  # noqa: E402

DATABASE_URL = os.environ.get("PRISM_DATABASE_URL", "").strip()
CLIENT = "https://claude.ai/oauth/claude-code-client-metadata"


@unittest.skipUnless(DATABASE_URL, "PRISM_DATABASE_URL is required")
class ClientNameTests(unittest.TestCase):
    def setUp(self) -> None:
        workspace_service.workspace.initialize()
        comment_client_names.clear_cache()
        self.addCleanup(comment_client_names.clear_cache)
        self.client_id = f"{CLIENT}?t={uuid.uuid4().hex[:8]}"
        with database.connection() as conn:
            conn.execute(
                """
                INSERT INTO workspace.ws_mcp_grants (id, client_id, client_name, user_email, scopes)
                VALUES (%s, %s, 'Claude Code', 'a@example.test', '["api:read"]')
                """,
                (uuid.uuid4().hex, self.client_id),
            )
            conn.commit()
        self.addCleanup(self._delete)

    def _delete(self) -> None:
        with database.connection() as conn:
            conn.execute("DELETE FROM workspace.ws_mcp_grants WHERE client_id = %s", (self.client_id,))
            conn.commit()

    def test_a_client_is_named_by_its_approved_grant(self) -> None:
        self.assertEqual(comment_client_names.name_for(self.client_id), "Claude Code")

    def test_names_are_cached(self) -> None:
        comment_client_names.name_for(self.client_id)
        with mock.patch.object(comment_client_names, "_lookup", side_effect=AssertionError("not cached")):
            self.assertEqual(comment_client_names.name_for(self.client_id), "Claude Code")

    def test_an_unknown_client_has_no_name(self) -> None:
        self.assertIsNone(comment_client_names.name_for("https://unknown.example/meta.json"))
        self.assertIsNone(comment_client_names.name_for(None))

    def test_a_failed_lookup_answers_none(self) -> None:
        with mock.patch.object(comment_client_names, "_lookup", side_effect=RuntimeError("db down")):
            self.assertIsNone(comment_client_names.name_for("https://other.example/meta.json"))


if __name__ == "__main__":
    unittest.main()
