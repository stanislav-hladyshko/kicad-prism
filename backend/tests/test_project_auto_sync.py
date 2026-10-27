from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.services import project_auto_sync_service  # noqa: E402
from support.repo import git  # noqa: E402


class ProjectAutoSyncTests(unittest.TestCase):
    def test_schedules_one_fetch_per_due_repository_and_throttles_retries(self) -> None:
        now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
        projects = [
            {"id": "a", "repo_id": "shared", "repo_last_synced": None},
            {"id": "b", "repo_id": "shared", "repo_last_synced": None},
            {"id": "c", "repo_id": "recent", "repo_last_synced": (now - timedelta(seconds=60)).isoformat()},
        ]
        attempts = {}
        with (
            mock.patch.object(project_auto_sync_service.workspace, "get_all_projects", return_value=projects),
            mock.patch.object(project_auto_sync_service, "start_sync_job") as start,
        ):
            self.assertEqual(project_auto_sync_service.enqueue_due_fetches(attempts, interval_seconds=300, now=now), 1)
            self.assertEqual(project_auto_sync_service.enqueue_due_fetches(attempts, interval_seconds=300, now=now), 0)
        start.assert_called_once_with("a", requested_by="system:auto-sync", fetch_only=True)

    def test_auto_sync_enqueues_a_fetch_only_job_under_the_read_lock(self) -> None:
        now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
        projects = [
            {"id": "a", "repo_id": "shared", "repo_last_synced": None, "path": "/repo"},
        ]
        with (
            mock.patch.object(
                project_auto_sync_service.workspace,
                "get_all_projects",
                return_value=projects,
            ),
            mock.patch.object(
                project_auto_sync_service.workspace,
                "get_project_by_id",
                return_value={"id": "a", "repo_id": "shared"},
            ),
            mock.patch.object(
                project_auto_sync_service, "remote_unchanged", return_value=False
            ),
            mock.patch(
                "app.services.job_service.jobs.enqueue",
                return_value={"job_id": "job-fetch"},
            ) as enqueue,
        ):
            self.assertEqual(
                project_auto_sync_service.enqueue_due_fetches(
                    {}, interval_seconds=300, now=now
                ),
                1,
            )
        self.assertEqual(enqueue.call_args.args[0], "project_fetch")
        self.assertEqual(enqueue.call_args.args[1], {"project_id": "a", "fetch_only": True})
        self.assertEqual(
            enqueue.call_args.kwargs["locks"],
            [{"key": "repository:shared", "mode": "read"}],
        )

    def test_an_unchanged_remote_gets_no_job_only_a_synced_time(self) -> None:
        now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
        projects = [{"id": "a", "repo_id": "quiet", "repo_last_synced": None, "path": "/repo"}]
        with (
            mock.patch.object(project_auto_sync_service.workspace, "get_all_projects", return_value=projects),
            mock.patch.object(project_auto_sync_service.workspace, "update_repository_synced") as synced,
            mock.patch.object(project_auto_sync_service, "remote_unchanged", return_value=True) as unchanged,
            mock.patch.object(project_auto_sync_service, "start_sync_job") as start,
        ):
            self.assertEqual(project_auto_sync_service.enqueue_due_fetches({}, interval_seconds=300, now=now), 0)
        unchanged.assert_called_once_with("/repo")
        synced.assert_called_once_with("quiet")
        start.assert_not_called()


class FastForwardTests(unittest.TestCase):
    """PRISM_AUTO_SYNC_FAST_FORWARD: the background job is the manual Sync, so the checkout follows the branch."""

    NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    PROJECTS = [{"id": "a", "repo_id": "shared", "repo_last_synced": None, "path": "/repo"}]

    def enqueue(self, *, unchanged: bool, behind: bool):
        with (
            mock.patch.object(project_auto_sync_service.settings, "PRISM_AUTO_SYNC_FAST_FORWARD", True),
            mock.patch.object(project_auto_sync_service.workspace, "get_all_projects", return_value=self.PROJECTS),
            mock.patch.object(project_auto_sync_service.workspace, "update_repository_synced") as synced,
            mock.patch.object(project_auto_sync_service, "remote_unchanged", return_value=unchanged),
            mock.patch.object(project_auto_sync_service, "checkout_behind", return_value=behind),
            mock.patch.object(project_auto_sync_service, "start_sync_job") as start,
        ):
            queued = project_auto_sync_service.enqueue_due_fetches({}, interval_seconds=300, now=self.NOW)
        return queued, start, synced

    def test_a_moved_remote_gets_a_full_sync(self) -> None:
        queued, start, _ = self.enqueue(unchanged=False, behind=False)
        self.assertEqual(queued, 1)
        start.assert_called_once_with("a", requested_by="system:auto-sync", fetch_only=False)

    def test_a_checkout_left_behind_by_a_fetch_gets_a_full_sync(self) -> None:
        queued, start, synced = self.enqueue(unchanged=True, behind=True)
        self.assertEqual(queued, 1)
        start.assert_called_once_with("a", requested_by="system:auto-sync", fetch_only=False)
        synced.assert_not_called()

    def test_an_up_to_date_checkout_gets_no_job(self) -> None:
        queued, start, synced = self.enqueue(unchanged=True, behind=False)
        self.assertEqual(queued, 0)
        start.assert_not_called()
        synced.assert_called_once_with("shared")


class RemoteUnchangedTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.origin = root / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        git(self.origin, "commit", "-q", "--allow-empty", "-m", "one")
        git(self.origin, "tag", "-a", "v1", "-m", "v1")
        git(root, "clone", "-q", str(self.origin), "clone")
        self.clone = root / "clone"

    def test_matches_after_a_fetch_and_not_after_an_upstream_commit(self) -> None:
        self.assertTrue(project_auto_sync_service.remote_unchanged(str(self.clone)))
        git(self.origin, "commit", "-q", "--allow-empty", "-m", "two")
        self.assertFalse(project_auto_sync_service.remote_unchanged(str(self.clone)))
        git(self.clone, "fetch", "-q", "--tags", "--prune")
        self.assertTrue(project_auto_sync_service.remote_unchanged(str(self.clone)))
        git(self.origin, "branch", "feature")
        self.assertFalse(project_auto_sync_service.remote_unchanged(str(self.clone)))

    def test_checkout_behind_until_the_branch_fast_forwards(self) -> None:
        self.assertFalse(project_auto_sync_service.checkout_behind(str(self.clone)))
        git(self.origin, "commit", "-q", "--allow-empty", "-m", "two")
        git(self.clone, "fetch", "-q")
        self.assertTrue(project_auto_sync_service.checkout_behind(str(self.clone)))
        git(self.clone, "merge", "-q", "--ff-only", "origin/main")
        self.assertFalse(project_auto_sync_service.checkout_behind(str(self.clone)))

    def test_detached_or_missing_checkout_is_not_behind(self) -> None:
        git(self.clone, "checkout", "-q", "--detach")
        self.assertFalse(project_auto_sync_service.checkout_behind(str(self.clone)))
        self.assertFalse(project_auto_sync_service.checkout_behind(""))

    def test_any_doubt_means_fetch(self) -> None:
        self.assertFalse(project_auto_sync_service.remote_unchanged(""))
        self.assertFalse(project_auto_sync_service.remote_unchanged(str(self.clone.parent / "missing")))


if __name__ == "__main__":
    unittest.main()
