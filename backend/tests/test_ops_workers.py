"""Worker heartbeats behind the ops diagnostics (ADR scripting-and-mcp §11, OPS-03).

``WorkerHeartbeatTests`` runs the real migration and SQL against a scratch
schema. ``WorkerHeartbeatScheduleTests`` checks that the supervision loop asks
for a heartbeat no more often than every fifteen seconds, and publishes only
the four per-job keys a result may carry.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import prism_worker  # noqa: E402
from app.services.ops import workers  # noqa: E402
from app.services.workspace_schema_migrations import WORKSPACE_LEDGER  # noqa: E402
from support.postgres import requires_scratch_postgres, scratch_schema  # noqa: E402

STARTED_AT = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


@requires_scratch_postgres("ops worker heartbeat tests")
class WorkerHeartbeatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = scratch_schema(
            self, "ops_workers", ledgers=(WORKSPACE_LEDGER,)
        )
        patcher = patch.object(workers, "connect", self.scratch.connection)
        patcher.start()
        self.addCleanup(patcher.stop)

    def heartbeat(self, worker_id: str = "prism:host:1:abc123", **overrides):
        values = {
            "pool": "prism",
            "host": "host",
            "pid": 1,
            "started_at": STARTED_AT,
            "concurrency": 4,
            "memory_budget_mib": 8192,
            "running_jobs": [
                {
                    "job_id": "job-1",
                    "kind": "design_bundle",
                    "started_at": STARTED_AT,
                    "pid": 4321,
                }
            ],
        }
        values.update(overrides)
        workers.heartbeat(worker_id, **values)

    def test_heartbeat_upserts_one_row_and_refreshes_running_jobs(self) -> None:
        self.heartbeat()
        self.heartbeat(
            running_jobs=[
                {
                    "job_id": "job-2",
                    "kind": "design_compare",
                    "started_at": STARTED_AT,
                    "pid": 9876,
                    "secret": "must not be published",
                }
            ]
        )

        [row] = workers.list_workers()

        self.assertEqual(row["worker_id"], "prism:host:1:abc123")
        self.assertEqual(row["pool"], "prism")
        self.assertEqual(row["host"], "host")
        self.assertEqual(row["pid"], 1)
        self.assertEqual(row["concurrency"], 4)
        self.assertEqual(row["memory_budget_mib"], 8192)
        self.assertEqual(row["started_at"], "2026-10-26T12:00:00Z")
        self.assertFalse(row["stale"])
        self.assertEqual(
            row["running_jobs"],
            [
                {
                    "job_id": "job-2",
                    "kind": "design_compare",
                    "started_at": "2026-10-26T12:00:00Z",
                    "pid": 9876,
                }
            ],
        )

    def test_a_row_unseen_for_the_stale_window_is_flagged(self) -> None:
        self.heartbeat()
        self.assertFalse(workers.list_workers(stale_after_seconds=120)[0]["stale"])

        with self.scratch.connect() as conn:
            conn.execute(
                "UPDATE ws_ops_workers SET last_seen_at = NOW() - INTERVAL '10 minutes'"
            )
            conn.commit()

        self.assertTrue(workers.list_workers(stale_after_seconds=120)[0]["stale"])
        self.assertFalse(workers.list_workers(stale_after_seconds=3600)[0]["stale"])

    def test_forget_removes_the_row(self) -> None:
        self.heartbeat()

        workers.forget("prism:host:1:abc123")

        self.assertEqual(workers.list_workers(), [])

    def test_heartbeat_prunes_rows_unseen_for_a_day(self) -> None:
        self.heartbeat("prism:host:1:old00000")
        with self.scratch.connect() as conn:
            conn.execute(
                "UPDATE ws_ops_workers SET last_seen_at = NOW() - INTERVAL '2 days'"
            )
            conn.commit()

        self.heartbeat("prism:host:1:new00000")

        self.assertEqual(
            [row["worker_id"] for row in workers.list_workers()],
            ["prism:host:1:new00000"],
        )

    def test_the_prune_is_bounded_per_heartbeat(self) -> None:
        with self.scratch.connect() as conn:
            conn.execute(
                """
                INSERT INTO ws_ops_workers
                    (worker_id, pool, host, pid, concurrency, memory_budget_mib,
                     running_jobs, last_seen_at)
                SELECT 'dead-' || value, 'prism', 'host', 1, 1, 512,
                       '[]'::jsonb, NOW() - INTERVAL '2 days'
                FROM generate_series(1, 150) AS value
                """
            )
            conn.commit()

        self.heartbeat("prism:host:1:live0000")
        self.assertEqual(len(workers.list_workers()), 51)

        self.heartbeat("prism:host:1:live0000")
        self.assertEqual(
            [row["worker_id"] for row in workers.list_workers()],
            ["prism:host:1:live0000"],
        )


class WorkerHeartbeatScheduleTests(unittest.TestCase):
    def test_the_supervision_loop_publishes_at_most_every_fifteen_seconds(self) -> None:
        worker = prism_worker.PrismWorker()
        process = mock.Mock(pid=4321)
        worker.running["job-1"] = prism_worker.RunningJob(
            job_id="job-1",
            fence=1,
            attempt=1,
            process=process,
            log_handle=mock.Mock(),
            last_heartbeat=0,
            kind="design_bundle",
            started_at=STARTED_AT,
        )

        with patch.object(prism_worker.time, "monotonic", return_value=100.0):
            with patch("app.services.ops.workers.heartbeat") as heartbeat:
                worker.schedule_worker_heartbeat()
                worker.schedule_worker_heartbeat()

        heartbeat.assert_called_once()

        args = heartbeat.call_args.args
        self.assertEqual(args[0], worker.worker_id)
        self.assertEqual(args[1], worker.worker_pool)
        self.assertEqual(args[5], worker.concurrency)
        self.assertEqual(
            args[7],
            [
                {
                    "job_id": "job-1",
                    "kind": "design_bundle",
                    "started_at": STARTED_AT,
                    "pid": 4321,
                }
            ],
        )

    def test_the_next_heartbeat_is_due_after_fifteen_seconds(self) -> None:
        worker = prism_worker.PrismWorker()
        with patch("app.services.ops.workers.heartbeat") as heartbeat:
            with patch.object(prism_worker.time, "monotonic", return_value=100.0):
                worker.schedule_worker_heartbeat()
            with patch.object(prism_worker.time, "monotonic", return_value=114.9):
                worker.schedule_worker_heartbeat()
            self.assertEqual(heartbeat.call_count, 1)
            with patch.object(prism_worker.time, "monotonic", return_value=115.0):
                worker.schedule_worker_heartbeat()
        self.assertEqual(heartbeat.call_count, 2)


if __name__ == "__main__":
    unittest.main()
