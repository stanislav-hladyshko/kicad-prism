"""Design-query revision resolution (ADR §3.3).

``None`` is the checkout's HEAD, a full 40-hex id is immutable and returned
lowercased without Git, and any other name is resolved by
``project_revision.resolve_commit``; an unknown name is a ``ValueError``.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.services import project_revision
from app.services.design_query import revision

COMMIT = "1234567890abcdef1234567890abcdef12345678"
FULL_SHA = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"


class RevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = SimpleNamespace(id="p1", path="/repo/project")
        self.location = SimpleNamespace(repo_root=Path("/repo"))
        self.locate = self.enterContext(
            mock.patch.object(revision.project_revision, "locate", return_value=self.location)
        )
        self.resolve_commit = self.enterContext(
            mock.patch.object(revision.project_revision, "resolve_commit", return_value=COMMIT)
        )

    def test_none_reads_the_checkout_head(self) -> None:
        self.assertEqual(revision.resolve(self.project, None), COMMIT)
        self.resolve_commit.assert_called_once_with(self.location, "HEAD")

    def test_a_full_sha_is_lowercased_without_git(self) -> None:
        self.assertEqual(revision.resolve(self.project, FULL_SHA), FULL_SHA.lower())
        self.resolve_commit.assert_not_called()
        self.locate.assert_not_called()

    def test_a_branch_is_resolved_against_the_repository(self) -> None:
        self.assertEqual(revision.resolve(self.project, "main"), COMMIT)
        self.resolve_commit.assert_called_once_with(self.location, "main")

    def test_a_short_sha_is_resolved_like_a_ref(self) -> None:
        self.assertEqual(revision.resolve(self.project, "abcdef0"), COMMIT)
        self.resolve_commit.assert_called_once_with(self.location, "abcdef0")

    def test_an_unknown_name_is_a_value_error(self) -> None:
        self.resolve_commit.side_effect = project_revision.RevisionNotFound("Commit not found: nope")
        with self.assertRaises(ValueError):
            revision.resolve(self.project, "nope")

    def test_only_a_full_sha_is_immutable(self) -> None:
        self.assertTrue(revision.is_full_sha(FULL_SHA))
        self.assertFalse(revision.is_full_sha("abcdef0"))
        self.assertFalse(revision.is_full_sha(None))


if __name__ == "__main__":
    unittest.main()
