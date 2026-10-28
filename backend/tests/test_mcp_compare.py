"""The comparison tools and the MCP tasks extension (ADR scripting-and-mcp §3.7).

The route tests drive the real application with a personal access token, as
``test_mcp_server.py`` does. A comparison is never run: the job start is stubbed
at the service seam the route calls, and ``get_changes`` reads the review route
over ``fixtures/review-groups/`` with its result loader stubbed. The tasks tests
stub the job row through the job service, so every status transition is covered
without a worker.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from app.api import design_compare_review  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.services import access_service, design_compare_service, personal_tokens  # noqa: E402
from app.services.job_service import jobs  # noqa: E402
from app.services.mcp import invoke, registry  # noqa: E402
from support.postgres import seed_folder, seed_project, seed_repository  # noqa: E402

DATABASE_URL = os.environ.get("PRISM_DATABASE_URL", "").strip()
FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "review-groups"
TEST_SECRET = "test-secret-at-least-32-characters-long-x"
BASE = "https://prism.test"
JOB_ID = "job-compare-1"
BASE_COMMIT = "a" * 40
HEAD_COMMIT = "b" * 40

TASKS_CAPABILITIES = {
    "io.modelcontextprotocol/clientCapabilities": {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }
}

try:
    import starlette.testclient  # noqa: F401
    HAS_TEST_CLIENT = True
except RuntimeError:
    HAS_TEST_CLIENT = False


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def use_designer() -> tuple[str, str]:
    email = f"{unique('mcp-compare')}@example.test"
    access_service.upsert_user(email=email, name=email.split("@")[0])
    access_service.upsert_user_role(email, "designer", updated_by=email)
    _, token = personal_tokens.create(
        email, "mcp compare tests", ["api:read", "design:write"], 30
    )
    return email, token


def app_connection():
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    conn.execute("SET search_path TO workspace, public")
    return conn


def fixture(name: str):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def initialize() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2026-07-28",
            "capabilities": {},
            "clientInfo": {"name": "tests", "version": "1"},
        },
    }


def call(tool: str, arguments: dict, request_id: int = 2) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }


def task_call(tool: str, arguments: dict, request_id: int = 3) -> dict:
    payload = call(tool, arguments, request_id)
    payload["params"]["_meta"] = TASKS_CAPABILITIES
    return payload


def task_get(task_id: str, request_id: int = 4) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tasks/get",
        "params": {"taskId": task_id},
    }


def task_cancel(task_id: str, request_id: int = 5) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tasks/cancel",
        "params": {"taskId": task_id},
    }


def job_row(
    *,
    project_id: str | None = None,
    status: str = "completed",
    kind: str = "design_compare",
    **extra,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "job_id": JOB_ID,
        "kind": kind,
        "status": status,
        "stage": None,
        "message": None,
        "percent": None,
        "attempt": 1,
        "max_attempts": 1,
        "project_id": project_id,
        "created_at": now,
        "updated_at": now,
        "result_metadata": {"base": BASE_COMMIT, "head": HEAD_COMMIT},
        "error_code": None,
        "error_message": None,
    }
    row.update(extra)
    return row


@unittest.skipUnless(DATABASE_URL, "PRISM_DATABASE_URL is required for the MCP comparison tests")
@unittest.skipUnless(HAS_TEST_CLIENT, "starlette.testclient needs httpx2 or httpx")
class McpCompareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from starlette.testclient import TestClient

        cls._patchers = [
            patch.object(settings, "AUTH_ENABLED_OVERRIDE", True),
            patch.object(settings, "PASSWORD_AUTH_ENABLED", True),
            patch.object(settings, "SESSION_SECRET", TEST_SECRET),
            patch.object(settings, "PUBLIC_BASE_URL", BASE),
            patch.object(settings, "PRISM_MCP_ENABLED", True),
        ]
        for patcher in cls._patchers:
            patcher.start()

        import app.main as main

        cls.main = main
        cls.TestClient = TestClient

    @classmethod
    def tearDownClass(cls) -> None:
        for patcher in getattr(cls, "_patchers", []):
            patcher.stop()

    def setUp(self) -> None:
        self.client = self.TestClient(self.main.app)
        self._repositories: list[str] = []
        self._projects: list[str] = []
        self._folders: list[str] = []
        self.addCleanup(self._cleanup)
        self.email, self.token = use_designer()
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def _cleanup(self) -> None:
        with app_connection() as conn:
            conn.execute("DELETE FROM ws_projects WHERE id = ANY(%s)", (self._projects,))
            conn.execute("DELETE FROM ws_repositories WHERE id = ANY(%s)", (self._repositories,))
            conn.execute("DELETE FROM ws_folders WHERE id = ANY(%s)", (self._folders,))
            conn.commit()

    def _project(self, *, hidden: bool = False) -> str:
        suffix = uuid.uuid4().hex[:10]
        repository = f"repo-{suffix}"
        project = f"project-{suffix}"
        folder = None
        with app_connection() as conn:
            seed_repository(conn, repository)
            if hidden:
                folder = f"folder-{suffix}"
                seed_folder(
                    conn,
                    folder,
                    visibility_mode="roles",
                    allowed_roles=["admin"],
                )
                self._folders.append(folder)
            seed_project(
                conn,
                project,
                repository,
                name=f"Board {suffix}",
                folder_id=folder,
            )
            conn.commit()
        self._repositories.append(repository)
        self._projects.append(project)
        return project

    def mcp(self, payload, **kwargs) -> dict:
        response = self.client.post("/mcp", json=payload, headers=kwargs.pop("headers", self.headers), **kwargs)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def review_stub(self, project: str, *, status: str = "completed", result=None):
        """The comparison route's status and result, stubbed at its own seam."""

        return _Patches(
            patch.object(
                design_compare_review,
                "get_project_for_role_or_404",
                return_value=SimpleNamespace(id=project),
            ),
            patch.object(
                design_compare_review.design_compare_service,
                "get_job_status",
                return_value={
                    "project_id": project,
                    "status": status,
                    "result_version": 1 if status == "completed" else 0,
                },
            ),
            patch.object(
                design_compare_review,
                "load_comparison_result",
                return_value=result,
            ),
        )

    # --------------------------------------------------------------- invoke

    def test_invoke_carries_a_post_body_and_the_callers_bearer(self) -> None:
        project = self._project()
        seen: dict = {}

        def fake_start(project_id, base, head, *, include_unchanged=False, requested_by=""):
            seen.update(
                project_id=project_id,
                base=base,
                head=head,
                include_unchanged=include_unchanged,
                requested_by=requested_by,
            )
            return "job-post-1"

        probe = registry.Tool(
            name="post_probe",
            title="POST probe",
            description="The comparison route, used to prove the body travels.",
            input_schema={
                "type": "object",
                "properties": {"project_id": {"type": "string"}},
            },
            route=("post", "/api/v1/projects/{project_id}/design-compare"),
            args=lambda arguments: (
                {"project_id": arguments["project_id"]},
                {},
                {
                    "base": BASE_COMMIT,
                    "head": HEAD_COMMIT,
                    "include_unchanged": True,
                },
            ),
            annotations=dict(registry.READ_ONLY_ANNOTATIONS),
        )

        with patch.object(design_compare_service, "start_design_compare_job", side_effect=fake_start):
            result = asyncio.run(
                invoke.call_tool(
                    self.main.app,
                    probe,
                    {"project_id": project},
                    authorization=f"Bearer {self.token}",
                )
            )

        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"], {"job_id": "job-post-1"})
        self.assertEqual(seen["project_id"], project)
        self.assertEqual(seen["base"], BASE_COMMIT)
        self.assertEqual(seen["head"], HEAD_COMMIT)
        self.assertTrue(seen["include_unchanged"])
        # The route required a viewer, so the caller's bearer reached it.
        self.assertEqual(seen["requested_by"], self.email)

    # ------------------------------------------------------ compare_revisions

    def test_compare_revisions_returns_a_job_handle(self) -> None:
        project = self._project()
        with patch.object(
            design_compare_service, "start_design_compare_job", return_value=JOB_ID
        ) as start:
            result = self.mcp(
                call(
                    "compare_revisions",
                    {"project_id": project, "base": BASE_COMMIT, "head": HEAD_COMMIT},
                ),
                headers=self.headers,
            )["result"]

        self.assertFalse(result["isError"], result)
        structured = result["structuredContent"]
        self.assertEqual(structured["jobId"], JOB_ID)
        self.assertEqual(structured["status"], "queued")
        self.assertEqual(structured["pollWith"], "get_job")
        self.assertEqual(structured["then"], "get_changes")
        self.assertIn(f"/project/{project}", structured["link"])
        self.assertIn("section=history", structured["link"])
        self.assertIn(f"base={BASE_COMMIT}", structured["link"])
        self.assertIn(f"compare={HEAD_COMMIT}", structured["link"])
        start.assert_called_once()

    # ----------------------------------------------------------- get_changes

    def _changes(self, project: str, arguments: dict) -> dict:
        result = self.mcp(
            call(
                "get_changes",
                {"project_id": project, "job_id": JOB_ID, **arguments},
            ),
            headers=self.headers,
        )["result"]
        return result

    def test_get_changes_flattens_the_review_groups_in_order(self) -> None:
        project = self._project()
        expected = fixture("expected.json")["sch"]
        with self.review_stub(project, result=fixture("input.json")):
            result = self._changes(project, {"domain": "schematic"})

        self.assertFalse(result["isError"], result)
        structured = result["structuredContent"]
        self.assertEqual(structured["jobId"], JOB_ID)
        self.assertEqual(structured["domain"], "schematic")
        self.assertEqual(structured["total"], len(expected))
        self.assertIsNone(structured["next"])
        self.assertEqual(
            [row["groupId"] for row in structured["rows"]],
            [group["id"] for group in expected],
        )
        self.assertEqual(
            [row["kind"] for row in structured["rows"]],
            [group["kind"] for group in expected],
        )
        self.assertEqual(
            [row["summary"] for row in structured["rows"]],
            [group["summary"] for group in expected],
        )
        self.assertEqual(structured["rows"][0]["references"], [])

        by_id = {row["groupId"]: row for row in structured["rows"]}
        self.assertEqual(by_id["schematic:components:part>0R~>10R~"]["references"], ["R15", "R16"])
        self.assertEqual(by_id["schematic:components:cmp:r13"]["references"], ["R13"])

    def test_get_changes_filters_and_pages_with_an_opaque_cursor(self) -> None:
        project = self._project()
        expected = fixture("expected.json")["sch"]
        removed = [group for group in expected if group["kind"] == "removed"]
        with self.review_stub(project, result=fixture("input.json")):
            filtered = self._changes(
                project, {"domain": "schematic", "kind": "removed"}
            )
            self.assertFalse(filtered["isError"], filtered)
            self.assertEqual(filtered["structuredContent"]["total"], len(removed))
            self.assertEqual(
                [row["groupId"] for row in filtered["structuredContent"]["rows"]],
                [group["id"] for group in removed],
            )

            by_reference = self._changes(
                project, {"domain": "schematic", "reference": "R13"}
            )
            self.assertFalse(by_reference["isError"], by_reference)
            self.assertEqual(by_reference["structuredContent"]["total"], 1)
            self.assertEqual(
                by_reference["structuredContent"]["rows"][0]["groupId"],
                "schematic:components:cmp:r13",
            )

            first = self._changes(project, {"domain": "schematic", "limit": 7})
            self.assertFalse(first["isError"], first)
            self.assertEqual(len(first["structuredContent"]["rows"]), 7)
            self.assertEqual(first["structuredContent"]["total"], len(expected))
            cursor = first["structuredContent"]["next"]
            self.assertIsNotNone(cursor)
            decoded = json.loads(base64.b64decode(cursor))
            self.assertEqual(decoded, {"job": JOB_ID, "offset": 7})

            second = self._changes(
                project, {"domain": "schematic", "limit": 7, "cursor": cursor}
            )
            self.assertFalse(second["isError"], second)
            self.assertEqual(
                [row["groupId"] for row in second["structuredContent"]["rows"]],
                [group["id"] for group in expected[7:14]],
            )

            last_cursor = base64.b64encode(
                json.dumps({"job": JOB_ID, "offset": len(expected)}).encode("utf-8")
            ).decode("ascii")
            last = self._changes(
                project, {"domain": "schematic", "limit": 7, "cursor": last_cursor}
            )
            self.assertEqual(last["structuredContent"]["rows"], [])
            self.assertIsNone(last["structuredContent"]["next"])
            self.assertEqual(last["structuredContent"]["total"], len(expected))

    def test_get_changes_is_deterministic(self) -> None:
        project = self._project()
        with self.review_stub(project, result=fixture("input.json")):
            first = self._changes(project, {"domain": "pcb"})
            second = self._changes(project, {"domain": "pcb"})
        self.assertEqual(first, second)

    def test_get_changes_maps_a_running_comparison_to_comparison_running(self) -> None:
        project = self._project()
        with self.review_stub(project, status="running"):
            with patch.object(jobs, "get", return_value=job_row(project_id=project, status="running")):
                result = self._changes(project, {"domain": "schematic"})

        self.assertTrue(result["isError"])
        error = result["structuredContent"]["error"]
        self.assertEqual(error["code"], "comparison-running")
        self.assertEqual(error["detail"], "Job status: running")

    # --------------------------------------------------------------- tasks

    def test_initialize_lists_the_tasks_extension(self) -> None:
        result = self.mcp(initialize(), headers=self.headers)["result"]
        self.assertEqual(
            result["capabilities"]["extensions"],
            {"io.modelcontextprotocol/tasks": {}},
        )

    def test_a_declared_call_returns_a_task_handle(self) -> None:
        project = self._project()
        with patch.object(
            design_compare_service, "start_design_compare_job", return_value=JOB_ID
        ):
            result = self.mcp(
                task_call(
                    "compare_revisions",
                    {"project_id": project, "base": BASE_COMMIT, "head": HEAD_COMMIT},
                ),
                headers=self.headers,
            )["result"]

        self.assertEqual(result["resultType"], "task")
        self.assertEqual(result["taskId"], JOB_ID)
        self.assertEqual(result["status"], "working")
        self.assertIsNone(result["ttlMs"])
        self.assertEqual(result["pollIntervalMs"], 2000)
        self.assertIsInstance(datetime.fromisoformat(result["createdAt"]), datetime)
        self.assertIsInstance(datetime.fromisoformat(result["lastUpdatedAt"]), datetime)
        self.assertNotIn("content", result)

    def test_an_undeclared_call_gets_the_plain_job_handle(self) -> None:
        project = self._project()
        with patch.object(
            design_compare_service, "start_design_compare_job", return_value=JOB_ID
        ):
            result = self.mcp(
                call(
                    "compare_revisions",
                    {"project_id": project, "base": BASE_COMMIT, "head": HEAD_COMMIT},
                ),
                headers=self.headers,
            )["result"]

        self.assertNotIn("resultType", result)
        self.assertEqual(result["structuredContent"]["jobId"], JOB_ID)

    def test_tasks_get_maps_every_job_status(self) -> None:
        project = self._project()
        cases = (
            ("queued", "working"),
            ("running", "working"),
            ("retry_wait", "working"),
            ("cancel_requested", "working"),
            ("cancelled", "cancelled"),
            ("completed", "completed"),
            ("failed", "completed"),
        )
        for job_status, task_status in cases:
            with self.subTest(job_status=job_status):
                row = job_row(project_id=project, status=job_status)
                if job_status == "failed":
                    row["error_code"] = "comparison-failed"
                    row["error_message"] = "The comparison failed."
                with patch.object(jobs, "get", return_value=row):
                    with self.review_stub(
                        project,
                        status=job_status,
                        result=fixture("input.json"),
                    ):
                        task = self.mcp(task_get(JOB_ID), headers=self.headers)["result"]

                self.assertEqual(task["resultType"], "complete")
                self.assertEqual(task["taskId"], JOB_ID)
                self.assertEqual(task["status"], task_status)
                self.assertIsNone(task["ttlMs"])
                if job_status == "completed":
                    summary = task["result"]["structuredContent"]
                    self.assertEqual(summary["jobId"], JOB_ID)
                    self.assertEqual(summary["base"], BASE_COMMIT)
                    self.assertEqual(summary["head"], HEAD_COMMIT)
                    self.assertIn(f"/project/{project}", summary["link"])
                    self.assertEqual(summary["domains"], {"sch": 40, "pcb": 32})
                    self.assertFalse(task["result"]["isError"])
                elif job_status == "failed":
                    self.assertTrue(task["result"]["isError"])
                    self.assertEqual(
                        task["result"]["structuredContent"]["error"],
                        {"code": "comparison-failed", "detail": "The comparison failed."},
                    )
                else:
                    self.assertNotIn("result", task)

    def test_an_unknown_or_hidden_task_is_minus_32602(self) -> None:
        project = self._project()
        with patch.object(jobs, "get", return_value=None):
            unknown = self.mcp(task_get("missing-task"), headers=self.headers)
        self.assertEqual(unknown["error"]["code"], -32602)

        with patch.object(
            jobs,
            "get",
            return_value=job_row(project_id=project, kind="design_bundle"),
        ):
            not_a_comparison = self.mcp(task_get(JOB_ID), headers=self.headers)
        self.assertEqual(not_a_comparison["error"]["code"], -32602)

        # A job on a project this caller cannot see is hidden, not found.
        hidden = self._project(hidden=True)
        with patch.object(
            jobs, "get", return_value=job_row(project_id=hidden)
        ):
            hidden_task = self.mcp(task_get("hidden-task"), headers=self.headers)
        self.assertEqual(hidden_task["error"]["code"], -32602)

    def test_tasks_cancel_calls_the_cancel_route(self) -> None:
        project = self._project()
        with patch.object(
            jobs, "get", return_value=job_row(project_id=project, status="running")
        ):
            with patch.object(jobs, "request_cancel", return_value="cancel_requested") as cancel:
                result = self.mcp(task_cancel(JOB_ID), headers=self.headers)["result"]

        self.assertEqual(result, {"resultType": "complete"})
        cancel.assert_called_once_with(JOB_ID, requested_by=self.email)


class _Patches:
    """A tiny context manager for several patchers, so tests read as one idea."""

    def __init__(self, *patchers):
        self._patchers = patchers

    def __enter__(self):
        for patcher in self._patchers:
            patcher.start()
        return self

    def __exit__(self, *exc):
        for patcher in reversed(self._patchers):
            patcher.stop()
        return False


if __name__ == "__main__":
    unittest.main()
