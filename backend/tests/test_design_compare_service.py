"""The shared design-compare helpers the bundle path and the routes keep (ADR §9.7 R1)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import design_compare_service, event_log
from app.services.workspace_schema_migrations import WORKSPACE_LEDGER
from support.postgres import requires_scratch_postgres, scratch_schema


class RevisionCacheTests(unittest.TestCase):
    def test_prune_evicts_oldest_over_the_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = []
            for index, project in enumerate(("pA", "pA", "pB")):
                commit_dir = root / project / f"commit{index}" / "tag"
                commit_dir.mkdir(parents=True)
                (commit_dir / "index.json").write_bytes(b"x" * 1000)
                stamp = 1_000 + index  # commit0 oldest
                os.utime(root / project / f"commit{index}", (stamp, stamp))
                entries.append(root / project / f"commit{index}")

            with mock.patch.object(design_compare_service, "_CACHE_ROOT", root):
                # Cap below the total forces one eviction, oldest first.
                design_compare_service._prune_revision_cache(max_bytes=2500)
                self.assertFalse(entries[0].exists())
                self.assertTrue(entries[1].exists())
                self.assertTrue(entries[2].exists())
                # A zero cap disables the prune.
                design_compare_service._prune_revision_cache(max_bytes=0)
                self.assertTrue(entries[1].exists())

    def test_eviction_ranks_by_last_use_not_by_creation(self) -> None:
        """The entry everyone diffs against is the oldest; it must not evict first."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, new = root / "pA" / "baseline", root / "pA" / "recent"
            for entry in (old, new):
                (entry / "tag" / "snapshot").mkdir(parents=True)
                (entry / "tag" / "snapshot" / "board.kicad_pcb").write_bytes(b"x" * 1000)
            os.utime(old, (1_000, 1_000))
            os.utime(new, (2_000, 2_000))

            with mock.patch.object(design_compare_service, "_CACHE_ROOT", root):
                # A build writes deep inside `new`; that must not count as use.
                (new / "tag" / "snapshot" / "extra.kicad_sch").write_bytes(b"y" * 10)
                self.assertEqual(new.stat().st_mtime, 2_000)

                # Resolving `old` for a comparison is what counts as use.
                design_compare_service._touch_cache_entry("pA", "baseline")
                self.assertGreater(old.stat().st_mtime, new.stat().st_mtime)

                design_compare_service._prune_revision_cache(max_bytes=1500, min_age_seconds=0)
                self.assertTrue(old.exists(), "the recently used baseline was evicted")
                self.assertFalse(new.exists())

    def test_entries_used_recently_are_never_evicted(self) -> None:
        """Prune runs in one worker while others may be mid-build on an entry."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = root / "pA" / "inflight" / "tag"
            entry.mkdir(parents=True)
            (entry / "index.json").write_bytes(b"x" * 4000)
            os.utime(root / "pA" / "inflight", None)  # stamped just now

            with mock.patch.object(design_compare_service, "_CACHE_ROOT", root):
                design_compare_service._prune_revision_cache(max_bytes=100, min_age_seconds=900)
                self.assertTrue((root / "pA" / "inflight").exists())


class ResolveRevisionTests(unittest.TestCase):
    def test_a_ref_resolves_to_a_full_immutable_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from support.repo import git, init_repo

            init_repo(tmp, {"a.txt": "a\n"})
            resolved = design_compare_service._resolve_revision(Path(tmp), "HEAD")
            self.assertEqual(resolved, git(tmp, "rev-parse", "HEAD"))

    def test_an_unknown_ref_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from support.repo import init_repo

            init_repo(tmp, {"a.txt": "a\n"})
            with self.assertRaises(ValueError):
                design_compare_service._resolve_revision(Path(tmp), "nope")


@requires_scratch_postgres("the comparison.done producer")
class ComparisonDoneEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events = scratch_schema(self, "compare_events", ledgers=(WORKSPACE_LEDGER,))
        self.enterContext(mock.patch.object(event_log, "connect", self.events.connection))

    def _events(self):
        with self.events.connect() as conn:
            return conn.execute("SELECT kind, project_id, topic, payload FROM ws_events").fetchall()

    def test_a_completed_comparison_appends_comparison_done(self) -> None:
        job = {
            "job_id": "job-1",
            "status": "completed",
            "project_id": "p1",
            "payload": {"base": "a" * 40, "head": "b" * 40},
            "result_metadata": {"result_version": 2, "base": "a" * 40, "head": "b" * 40},
        }

        self.assertFalse(design_compare_service.settle_comparison_end(job))

        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "comparison.done")
        self.assertEqual(events[0]["project_id"], "p1")
        self.assertEqual(events[0]["topic"], "job:job-1")
        self.assertEqual(
            events[0]["payload"],
            {"jobId": "job-1", "base": "a" * 40, "compare": "b" * 40},
        )

    def test_an_ended_comparison_that_did_not_complete_appends_nothing(self) -> None:
        job = {
            "job_id": "job-1",
            "status": "failed",
            "project_id": "p1",
            "payload": {"base": "a" * 40, "head": "b" * 40},
            "result_metadata": {"result_version": 1},
        }
        self.assertFalse(design_compare_service.settle_comparison_end(job))
        self.assertEqual(self._events(), [])


if __name__ == "__main__":
    unittest.main()
