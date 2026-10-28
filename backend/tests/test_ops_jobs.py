"""Ops job diagnostics (ADR scripting-and-mcp §11, OPS-04a).

The timeline is built from the rows the queue writes: ``ws_jobs``,
``ws_job_events``, the resource slots and locks it holds, and the job's log.
The bundle profile is built from ``ws_design_bundles`` and the bundle job's
``result_metadata``. Neither answer may carry design content: only ids, keys,
timings, counts and sizes, with every free-text field redacted.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.services import access_service, personal_tokens  # noqa: E402
from app.services.mcp import registry  # noqa: E402
from app.services.mcp import tools as _mcp_tools  # noqa: E402,F401
from app.services.ops.bundle_profile import bundle_build_profile  # noqa: E402
from app.services.ops import job_timeline as timeline_rules  # noqa: E402
from app.services.ops.job_timeline import job_timeline  # noqa: E402
from support.postgres import seed_job, seed_project, seed_repository  # noqa: E402

DATABASE_URL = os.environ.get("PRISM_DATABASE_URL", "").strip()
COMMIT = "0123456789abcdef0123456789abcdef01234567"
BUNDLE_KEY = "a" * 64
REVISION_KEY = "b" * 64
GENERATOR_DIGEST = "c" * 64

try:
    import starlette.testclient  # noqa: F401
    HAS_TEST_CLIENT = True
except RuntimeError:
    HAS_TEST_CLIENT = False


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def app_connection():
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    conn.execute("SET search_path TO workspace, public")
    return conn


def cleanup(jobs=(), projects=(), repositories=()) -> None:
    with app_connection() as conn:
        if jobs:
            conn.execute("DELETE FROM ws_job_resource_slots WHERE job_id = ANY(%s)", (list(jobs),))
            conn.execute("DELETE FROM ws_job_locks WHERE job_id = ANY(%s)", (list(jobs),))
            conn.execute("DELETE FROM ws_jobs WHERE id = ANY(%s)", (list(jobs),))
        if projects:
            conn.execute("DELETE FROM ws_design_bundles WHERE project_id = ANY(%s)", (list(projects),))
            conn.execute("DELETE FROM ws_projects WHERE id = ANY(%s)", (list(projects),))
        if repositories:
            conn.execute("DELETE FROM ws_repositories WHERE id = ANY(%s)", (list(repositories),))
        conn.commit()


def seed_project_tree() -> tuple[str, str]:
    repository, project = unique("repo"), unique("project")
    with app_connection() as conn:
        seed_repository(conn, repository)
        seed_project(conn, project, repository)
        conn.commit()
    return repository, project


def seed_event(conn, job: str, event_type: str, at: datetime, *, stage: str = "", percent=None, details=None) -> None:
    conn.execute(
        """INSERT INTO ws_job_events (job_id, event_type, stage, percent, details, created_at)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (job, event_type, stage, percent, Jsonb(details or {}), at),
    )


class RegistryTests(unittest.TestCase):
    def test_ops_tools_are_listed_only_while_ops_is_on(self) -> None:
        with patch.object(settings, "PRISM_MCP_OPS_ENABLED", False):
            names = {tool.name for tool in registry.tools()}
        self.assertNotIn("get_job_timeline", names)
        self.assertNotIn("get_bundle_build_profile", names)

        with patch.object(settings, "PRISM_MCP_OPS_ENABLED", True):
            names = {tool.name for tool in registry.tools()}
            timeline = registry.tool_named("get_job_timeline")
            profile = registry.tool_named("get_bundle_build_profile")
        self.assertIn("get_job_timeline", names)
        self.assertIn("get_bundle_build_profile", names)
        assert timeline is not None
        self.assertTrue(timeline.ops)
        self.assertFalse(timeline.write)
        self.assertEqual(timeline.action, "ops_read")
        self.assertEqual(timeline.route, ("get", "/api/v1/ops/jobs/{job_id}/timeline"))
        self.assertEqual(
            timeline.args({"job_id": "j", "log_lines": 5}),
            ({"job_id": "j"}, {"log_lines": 5}),
        )

        assert profile is not None
        self.assertEqual(profile.route, ("get", "/api/v1/ops/projects/{project_id}/bundle-profile"))
        self.assertEqual(
            profile.args({"project_id": "p", "commit": "c", "job_id": "j"}),
            ({"project_id": "p"}, {"commit": "c", "job_id": "j"}),
        )


@unittest.skipUnless(DATABASE_URL, "PRISM_DATABASE_URL is required")
class TimelineTests(unittest.TestCase):
    def test_phases_redaction_exit_code_and_log_tail(self) -> None:
        job, project = unique("job"), unique("project")
        started = datetime.now(timezone.utc) - timedelta(minutes=2)
        first_claim = started + timedelta(seconds=2)
        reclaim = first_claim + timedelta(seconds=8)
        finished = started + timedelta(seconds=20)
        self.addCleanup(cleanup, jobs=[job])

        with app_connection() as conn:
            seed_job(
                conn,
                job,
                "design_bundle",
                "failed",
                project_id=project,
                worker_pool="prism",
                priority=10,
                attempt=2,
                max_attempts=3,
                fence=2,
                lease_owner="worker-2",
                created_at=started,
                updated_at=finished,
                started_at=first_claim,
                completed_at=finished,
                error_code="runner_exited",
                error_message=(
                    "Job runner exited with code 3 while cloning "
                    "https://bot:hunter2@git.example/x.git"
                ),
                payload=Jsonb({"project_id": project, "field": "DESIGN-FIELD-VALUE"}),
                result_metadata=Jsonb({"project_id": project, "field": "DESIGN-FIELD-VALUE"}),
            )
            seed_event(conn, job, "queued", started)
            seed_event(
                conn,
                job,
                "claimed",
                first_claim,
                details={"worker_id": "worker-1", "fence": 1, "previous_fence": 0},
            )
            seed_event(conn, job, "progress", first_claim + timedelta(seconds=1), stage="bundle-key", percent=2)
            seed_event(conn, job, "progress", first_claim + timedelta(seconds=2), stage="bundle-parse", percent=15)
            seed_event(
                conn,
                job,
                "lease_reclaimed",
                reclaim,
                details={"worker_id": "worker-2", "fence": 2, "previous_fence": 1},
            )
            seed_event(conn, job, "progress", reclaim + timedelta(seconds=1), stage="bundle-parse", percent=60)
            seed_event(
                conn,
                job,
                "failed",
                finished,
                details={"error_code": "runner_exited", "error_message": "token=s3cret"},
            )
            conn.execute(
                """INSERT INTO ws_job_resource_slots
                   (resource_name, slot_number, job_id, fence, lease_owner, lease_expires_at, updated_at)
                   VALUES (%s, 1, %s, 2, 'worker-2', %s, %s)""",
                (f"resource-{job}", job, finished + timedelta(minutes=1), finished),
            )
            conn.execute(
                """INSERT INTO ws_job_locks
                   (lock_key, job_id, fence, mode, lease_owner, lease_expires_at, updated_at)
                   VALUES (%s, %s, 2, 'write', 'worker-2', %s, %s)""",
                (f"lock-{job}", job, finished + timedelta(minutes=1), finished),
            )
            conn.commit()

        with tempfile.TemporaryDirectory() as root:
            log_dir = Path(root) / "jobs"
            log_dir.mkdir()
            (log_dir / "job.log").write_text(
                "\n".join(
                    [
                        "cloning https://bot:hunter2@git.example/x.git",
                        "Authorization: Bearer abc.def",
                        "token=s3cret",
                        "plain line",
                    ]
                ),
                encoding="utf-8",
            )
            with app_connection() as conn:
                conn.execute("UPDATE ws_jobs SET log_path = %s WHERE id = %s", (str(log_dir / "job.log"), job))
                conn.commit()
            with patch.object(settings, "PRISM_JOB_ARTIFACT_ROOT", root):
                result = job_timeline(job, log_lines=10)

        assert result is not None
        summary = result["job"]
        self.assertEqual(summary["job_id"], job)
        self.assertEqual(summary["exit_code"], 3)
        self.assertIn("exited with code 3", summary["error_message"])
        self.assertNotIn("hunter2", summary["error_message"])

        queue = result["phases"]["queue_wait"]
        self.assertEqual(queue["seconds"], 2.0)

        claims = result["phases"]["claims"]
        self.assertEqual([claim["worker_id"] for claim in claims], ["worker-1", "worker-2"])
        self.assertEqual([claim["seconds"] for claim in claims], [8.0, 10.0])
        self.assertEqual([claim["outcome"] for claim in claims], ["lease_reclaimed", "failed"])

        stages = result["phases"]["stages"]
        # The reclaim ends worker-1's attempt, so its bundle-parse run stops
        # there instead of absorbing the time before worker-2 took over.
        self.assertEqual(
            [stage["stage"] for stage in stages], ["bundle-key", "bundle-parse", "bundle-parse"]
        )
        self.assertEqual([stage["seconds"] for stage in stages], [1.0, 6.0, 9.0])

        failed = next(event for event in result["events"] if event["event_type"] == "failed")
        self.assertNotIn("s3cret", json.dumps(failed))
        self.assertEqual(failed["details"]["error_code"], "runner_exited")

        self.assertEqual(result["log_tail"][0], "cloning https://[REDACTED]@git.example/x.git")
        self.assertEqual(result["log_tail"][1], "Authorization: [REDACTED]")
        self.assertEqual(result["log_tail"][2], "token=[REDACTED]")
        self.assertEqual(result["log_tail"][3], "plain line")

        self.assertEqual(result["payload_keys"], ["field", "project_id"])
        self.assertEqual(result["result_metadata_keys"], ["field", "project_id"])
        self.assertNotIn("DESIGN-FIELD-VALUE", json.dumps(result))

        self.assertEqual([slot["resource_name"] for slot in result["resource_slots"]], [f"resource-{job}"])
        self.assertEqual([lock["lock_key"] for lock in result["locks"]], [f"lock-{job}"])

    def test_events_are_capped_and_truncation_is_reported(self) -> None:
        job = unique("job")
        self.addCleanup(cleanup, jobs=[job])
        at = datetime.now(timezone.utc) - timedelta(minutes=5)
        with app_connection() as conn:
            seed_job(conn, job, "design_bundle", "running", created_at=at, updated_at=at, started_at=at)
            conn.execute(
                """INSERT INTO ws_job_events (job_id, event_type, details, created_at)
                   SELECT %s, 'progress', '{}'::jsonb, %s FROM generate_series(1, 501)""",
                (job, at),
            )
            conn.commit()

        result = job_timeline(job)
        assert result is not None
        self.assertTrue(result["events_truncated"])
        self.assertEqual(len(result["events"]), 500)

    def test_an_unknown_job_is_none(self) -> None:
        self.assertIsNone(job_timeline(unique("missing")))


@unittest.skipUnless(DATABASE_URL, "PRISM_DATABASE_URL is required")
class BundleProfileTests(unittest.TestCase):
    def test_profile_reduces_invocations_and_reports_counts(self) -> None:
        from app.api._helpers import get_project_for_role_or_404

        repository, project = seed_project_tree()
        job = unique("job")
        self.addCleanup(cleanup, jobs=[job], projects=[project], repositories=[repository])
        created = datetime.now(timezone.utc) - timedelta(minutes=3)
        started = created + timedelta(seconds=5)
        completed = started + timedelta(seconds=60)
        metadata = {
            "project_id": project,
            "commit": COMMIT,
            "invocations": [
                {
                    "command": "/opt/prism/prism-kicad bundle build --source-root /secret/board",
                    "exit": 0,
                    "elapsed_ms": 4200,
                    "peak_rss_bytes": 300000,
                },
                {"command": "bundle verify", "stage": "board", "exit": 0, "elapsed_ms": 500, "peak_rss_bytes": 20000},
            ],
            "kicad_steps": [
                {"step": "upgrade", "runs": 2, "exit": 0, "elapsed_ms": 1000, "peak_rss_bytes": 100},
                {"step": "glb", "runs": 1, "exit": 0, "elapsed_ms": 9000, "peak_rss_bytes": 400},
            ],
            "upgrade_cache_hits": 3,
            "upgrade_cache_misses": 1,
            "refill_cache_hit": True,
            "models_cache_hit": False,
            "stages": {"parse": "complete", "board": "complete", "models": "complete"},
            "failed_stages": [],
            "partial_stages": [],
            "stderr": ["Authorization: Bearer abc.def"],
        }
        with app_connection() as conn:
            seed_job(
                conn,
                job,
                "design_bundle",
                "completed",
                project_id=project,
                created_at=created,
                updated_at=completed,
                started_at=started,
                completed_at=completed,
                result_metadata=Jsonb(metadata),
            )
            conn.execute(
                """INSERT INTO ws_design_bundles
                   (project_id, bundle_key, revision_key, generator_digest, commit_sha, state,
                    stages, job_id, error, archive_bytes, created_at, updated_at, last_used_at)
                   VALUES (%s, %s, %s, %s, %s, 'complete', %s, %s, %s, 987654, %s, %s, %s)""",
                (
                    project,
                    BUNDLE_KEY,
                    REVISION_KEY,
                    GENERATOR_DIGEST,
                    COMMIT,
                    Jsonb({"parse": "complete", "board": "complete", "models": "complete"}),
                    job,
                    "bundle failed at https://bot:hunter2@git.example/x.git",
                    started,
                    completed,
                    completed,
                ),
            )
            conn.commit()

        project_row = get_project_for_role_or_404(project, "admin")
        profile = bundle_build_profile(project_row, job_id=job)
        assert profile is not None

        self.assertEqual(profile["project_id"], project)
        self.assertEqual(profile["commit"], COMMIT)
        self.assertEqual(profile["state"], "complete")
        self.assertEqual(profile["job_id"], job)
        self.assertEqual(profile["job_status"], "completed")
        self.assertEqual(profile["archive_bytes"], 987654)
        self.assertEqual(profile["wall_seconds"], 60.0)
        self.assertEqual(profile["queue_wait_seconds"], 5.0)
        self.assertEqual(profile["slowest_step"], "glb")

        self.assertEqual(
            profile["invocations"][0],
            {
                "executable": "prism-kicad",
                "subcommand": "bundle build",
                "stage": None,
                "exit": 0,
                "elapsed_ms": 4200.0,
                "peak_rss_bytes": 300000,
            },
        )
        self.assertEqual(profile["invocations"][1]["subcommand"], "bundle verify")
        self.assertEqual(profile["invocations"][1]["stage"], "board")
        self.assertEqual(
            profile["cache"],
            {
                "upgrade_cache_hits": 3,
                "upgrade_cache_misses": 1,
                "refill_cache_hit": True,
                "models_cache_hit": False,
            },
        )
        self.assertEqual(profile["stages"], {"parse": "complete", "board": "complete", "models": "complete"})
        self.assertEqual(profile["failed_stages"], [])
        self.assertEqual(profile["partial_stages"], [])
        self.assertNotIn("hunter2", profile["error"])

        rendered = json.dumps(profile)
        self.assertNotIn("/secret/board", rendered)
        self.assertNotIn("--source-root", rendered)
        self.assertNotIn("abc.def", rendered)
        self.assertNotIn("stderr", profile)

    def test_an_unknown_bundle_is_none(self) -> None:
        from app.api._helpers import get_project_for_role_or_404

        repository, project = seed_project_tree()
        self.addCleanup(cleanup, projects=[project], repositories=[repository])
        project_row = get_project_for_role_or_404(project, "admin")
        self.assertIsNone(bundle_build_profile(project_row, job_id=unique("missing")))


@unittest.skipUnless(DATABASE_URL, "PRISM_DATABASE_URL is required")
@unittest.skipUnless(HAS_TEST_CLIENT, "starlette.testclient needs httpx")
class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from starlette.testclient import TestClient

        cls._patchers = [
            patch.object(settings, "AUTH_ENABLED_OVERRIDE", True),
            patch.object(settings, "PASSWORD_AUTH_ENABLED", True),
            patch.object(settings, "SESSION_SECRET", "test-secret-at-least-32-characters-long-x"),
            patch.object(settings, "PUBLIC_BASE_URL", "https://prism.test"),
        ]
        for patcher in cls._patchers:
            patcher.start()
        import app.main as main

        cls.client = TestClient(main.app)

    @classmethod
    def tearDownClass(cls) -> None:
        for patcher in cls._patchers:
            patcher.stop()

    def _token(self, role: str, scopes: list[str]) -> dict:
        email = f"ops-{uuid.uuid4().hex[:8]}@example.test"
        access_service.upsert_user(email=email, name="ops")
        access_service.upsert_user_role(email, role, updated_by=email)
        _, token = personal_tokens.create(email, "ops", scopes, 30)
        return {"Authorization": f"Bearer {token}"}

    def _job(self) -> str:
        job = unique("job")
        self.addCleanup(cleanup, jobs=[job])
        at = datetime.now(timezone.utc) - timedelta(minutes=1)
        with app_connection() as conn:
            seed_job(
                conn,
                job,
                "design_bundle",
                "failed",
                created_at=at,
                updated_at=at + timedelta(seconds=10),
                started_at=at + timedelta(seconds=1),
                completed_at=at + timedelta(seconds=10),
                error_code="runner_exited",
                error_message="Job runner exited with code 9",
            )
            seed_event(conn, job, "claimed", at + timedelta(seconds=1), details={"worker_id": "w1", "fence": 1})
            seed_event(conn, job, "failed", at + timedelta(seconds=10))
            conn.commit()
        return job

    def _bundle(self) -> tuple[str, str]:
        repository, project = seed_project_tree()
        job = unique("job")
        self.addCleanup(cleanup, jobs=[job], projects=[project], repositories=[repository])
        with app_connection() as conn:
            seed_job(conn, job, "design_bundle", "completed", project_id=project)
            conn.execute(
                """INSERT INTO ws_design_bundles
                   (project_id, bundle_key, revision_key, generator_digest, commit_sha, state,
                    stages, job_id, archive_bytes)
                   VALUES (%s, %s, %s, %s, %s, 'complete', %s, %s, 1234)""",
                (
                    project,
                    BUNDLE_KEY,
                    REVISION_KEY,
                    GENERATOR_DIGEST,
                    COMMIT,
                    Jsonb({"parse": "complete"}),
                    job,
                ),
            )
            conn.commit()
        return project, job

    def test_routes_are_absent_while_ops_is_off(self) -> None:
        job = self._job()
        headers = self._token("admin", ["ops:read"])
        with patch.object(settings, "PRISM_MCP_OPS_ENABLED", False):
            timeline = self.client.get(f"/api/v1/ops/jobs/{job}/timeline", headers=headers)
            profile = self.client.get(
                f"/api/v1/ops/projects/{unique('project')}/bundle-profile", headers=headers
            )
        self.assertEqual(timeline.status_code, 404)
        self.assertEqual(profile.status_code, 404)

    def test_an_admin_reads_the_timeline_and_the_bundle_profile(self) -> None:
        job = self._job()
        project, bundle_job = self._bundle()
        headers = self._token("admin", ["ops:read"])
        with patch.object(settings, "PRISM_MCP_OPS_ENABLED", True):
            timeline = self.client.get(f"/api/v1/ops/jobs/{job}/timeline?log_lines=20", headers=headers)
            profile = self.client.get(
                f"/api/v1/ops/projects/{project}/bundle-profile?job_id={bundle_job}",
                headers=headers,
            )
        self.assertEqual(timeline.status_code, 200, timeline.text)
        body = timeline.json()
        self.assertEqual(body["job"]["job_id"], job)
        self.assertEqual(body["job"]["exit_code"], 9)
        self.assertEqual(body["phases"]["queue_wait"]["seconds"], 1.0)
        self.assertEqual(body["events"][0]["event_type"], "claimed")

        self.assertEqual(profile.status_code, 200, profile.text)
        bundle = profile.json()
        self.assertEqual(bundle["project_id"], project)
        self.assertEqual(bundle["job_id"], bundle_job)
        self.assertEqual(bundle["state"], "complete")

    def test_the_route_answers_typed_not_found_codes(self) -> None:
        headers = self._token("admin", ["ops:read"])
        with patch.object(settings, "PRISM_MCP_OPS_ENABLED", True):
            missing_job = self.client.get(
                f"/api/v1/ops/jobs/{unique('missing')}/timeline", headers=headers
            )
            # The project lookup runs before the bundle lookup, so seed one that exists.
            repository, seeded = seed_project_tree()
            self.addCleanup(cleanup, projects=[seeded], repositories=[repository])
            missing_bundle = self.client.get(
                f"/api/v1/ops/projects/{seeded}/bundle-profile?job_id={unique('missing')}",
                headers=headers,
            )
        self.assertEqual(missing_job.status_code, 404)
        self.assertEqual(missing_job.json()["code"], "job-not-found")
        self.assertEqual(missing_bundle.status_code, 404)
        self.assertEqual(missing_bundle.json()["code"], "bundle-not-found")

    def test_a_non_admin_or_a_token_without_ops_read_is_refused(self) -> None:
        job = self._job()
        designer = self._token("designer", ["ops:read"])
        wrong_scope = self._token("admin", ["api:write"])
        with patch.object(settings, "PRISM_MCP_OPS_ENABLED", True):
            not_admin = self.client.get(f"/api/v1/ops/jobs/{job}/timeline", headers=designer)
            no_scope = self.client.get(f"/api/v1/ops/jobs/{job}/timeline", headers=wrong_scope)
        self.assertEqual(not_admin.status_code, 403)
        self.assertEqual(no_scope.status_code, 403)


class TimelineBoundaryTests(unittest.TestCase):
    """The sequence the live check ran: fail, ops requeue, claim, cancel."""

    def test_a_requeue_and_a_cancel_request_split_attempts_and_stages_correctly(self) -> None:
        start = datetime(2026, 10, 26, 17, 45, tzinfo=timezone.utc)

        def at(seconds: float) -> datetime:
            return start + timedelta(seconds=seconds)

        events = [
            {"event_type": "claimed", "details": {"worker_id": "w1", "fence": 1}, "created_at": at(0)},
            {"event_type": "progress", "stage": "bundle-materialize", "created_at": at(1)},
            {"event_type": "failed", "created_at": at(4)},
            {"event_type": "requeued", "created_at": at(6000)},
            {"event_type": "claimed", "details": {"worker_id": "w2", "fence": 3}, "created_at": at(6001)},
            {"event_type": "progress", "stage": "bundle-materialize", "created_at": at(6002)},
            {"event_type": "cancel_requested", "created_at": at(6003)},
            {"event_type": "cancelled", "created_at": at(6005)},
        ]
        job = {"status": "cancelled", "completed_at": at(6005), "updated_at": at(6005)}

        stages = timeline_rules._stage_runs(job, events)
        self.assertEqual([(run["stage"], run["seconds"]) for run in stages],
                         [("bundle-materialize", 3.0), ("bundle-materialize", 3.0)])

        attempts = timeline_rules._claim_attempts(job, events)
        self.assertEqual([(a["worker_id"], a["outcome"], a["seconds"]) for a in attempts],
                         [("w1", "failed", 4.0), ("w2", "cancelled", 4.0)])


class LogTailInternalsTests(unittest.TestCase):
    """The shape the live check saw: a worker traceback and a scratch path."""

    def test_a_traceback_keeps_only_its_exception_line(self) -> None:
        lines = [
            "starting bundle",
            "Traceback (most recent call last):",
            '  File "/app/app/services/design_bundle/jobs.py", line 612, in run',
            "    archive = self.parse()",
            "              ^^^^^^^^^^^^",
            '  File "/app/app/services/design_bundle/kicad_steps.py", line 289, in upgrade',
            "    raise KicadSourceUnreadable(message)",
            "KicadSourceUnreadable: KiCad cannot read vme-wren.kicad_pcb",
            "job failed",
        ]
        self.assertEqual(
            timeline_rules._without_tracebacks(lines),
            [
                "starting bundle",
                "Traceback (frames omitted)",
                "KicadSourceUnreadable: KiCad cannot read vme-wren.kicad_pcb",
                "job failed",
            ],
        )

    def test_a_tail_that_starts_inside_a_traceback_is_reduced_too(self) -> None:
        lines = ['  File "/app/app/x.py", line 3, in f', "    g()", "ValueError: bad"]
        self.assertEqual(
            timeline_rules._without_tracebacks(lines), ["Traceback (frames omitted)", "ValueError: bad"]
        )

    def test_absolute_paths_keep_their_file_name_and_urls_are_untouched(self) -> None:
        message = (
            "Got 'symbol' in '/tmp/kicad-upgrade-7s4hl_g_/vme-wren.kicad_pcb', line 1453222; "
            "see https://gitlab.example/group/repo.git and /srv/prism-projects/a/b"
        )
        self.assertEqual(
            timeline_rules._without_paths(message),
            "Got 'symbol' in 'vme-wren.kicad_pcb', line 1453222; "
            "see https://gitlab.example/group/repo.git and b",
        )


if __name__ == "__main__":
    unittest.main()
