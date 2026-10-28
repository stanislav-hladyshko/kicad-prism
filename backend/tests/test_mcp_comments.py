"""The comment write tools (ADR scripting-and-mcp §10 D13–D16).

The routes are faked here: what is under test is how ``add_comment`` chooses
the anchored or comparison route, what body it sends, how a job supplies a
comparison's base and head, the citation it adds, and the resolve handoff the
server turns into a URL elicitation. The real routes are covered by
``test_comments_anchored`` and the MCP server test.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

from app.api import mcp as mcp_api
from app.core import policy
from app.core.config import settings
from app.core.security import AuthenticatedUser
from app.services.mcp import audit, governance, invoke, registry
from app.services.mcp import tools as mcp_tools  # noqa: F401 - registration
from app.services.mcp import tools_comments

COMMIT = "a" * 40

COMMENT = {
    "id": "c1",
    "context": "PCB",
    "scope": "canvas",
    "anchor": {"commit": COMMIT},
}
COMPARISON_COMMENT = {
    "id": "c2",
    "context": "SCH",
    "scope": "comparison",
    "baseCommit": "b" * 40,
    "compareCommit": "c" * 40,
    "anchor": {"commit": "c" * 40},
}


class FakeRoutes:
    """Answers a route by ``(method, path)`` and records every request."""

    def __init__(self, answers: dict[tuple[str, str], dict]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, dict | None]] = []

    async def __call__(self, app, method, path, query, authorization, body):
        self.calls.append((method, path, body))
        answer = self.answers.get((method, path))
        if answer is None:
            return 404, json.dumps({"detail": "not found", "code": "not_found"}).encode()
        return 201, json.dumps(answer).encode()


def canvas_routes() -> FakeRoutes:
    return FakeRoutes({("post", "/api/v1/projects/p1/comments/anchored"): COMMENT})


class SchemaTests(unittest.TestCase):
    def test_add_comment_names_every_anchor_and_needs_a_scope(self) -> None:
        tool = registry.tool_named("add_comment")
        assert tool is not None
        self.assertEqual(tool.action, "comment")
        self.assertFalse(tool.annotations["readOnlyHint"])
        properties = tool.input_schema["properties"]
        self.assertEqual({"component", "net", "sheet", "change", "commit", "view"} - set(properties), set())
        self.assertEqual(tool.input_schema["required"], ["project_id", "content"])

    def test_resolve_comment_is_a_write_scope_handoff_with_no_route_write(self) -> None:
        tool = registry.tool_named("resolve_comment")
        assert tool is not None
        self.assertEqual(tool.action, "comment")
        self.assertTrue(tool.annotations["idempotentHint"])
        self.assertIsNotNone(tool.execute)


class AddCommentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch.object(settings, "PUBLIC_BASE_URL", "https://prism.test"))

    def test_a_component_comment_goes_to_the_anchored_route(self) -> None:
        routes = canvas_routes()
        result = asyncio.run(
            tools_comments.add_comment(
                None,
                {
                    "project_id": "p1",
                    "content": "Check the clearance",
                    "component": "R12",
                    "commit": COMMIT,
                    "severity": "major",
                },
                "Bearer t",
                routes,
            )
        )
        self.assertFalse(result["isError"])
        method, path, body = routes.calls[0]
        self.assertEqual((method, path), ("post", "/api/v1/projects/p1/comments/anchored"))
        self.assertEqual(
            body,
            {
                "content": "Check the clearance",
                "commit": COMMIT,
                "component": "R12",
                "severity": "major",
            },
        )
        structured = result["structuredContent"]
        self.assertEqual(structured["id"], "c1")
        self.assertIn("section=visualizers", structured["link"])
        self.assertIn("tab=pcb", structured["link"])
        self.assertIn(f"commit={COMMIT}", structured["link"])
        self.assertIn("comment=c1", structured["link"])
        self.assertNotIn("uri", structured)

    def test_a_comparison_change_with_a_job_reads_its_commits(self) -> None:
        routes = FakeRoutes(
            {
                ("get", "/api/v1/jobs/job-1"): {
                    "job_id": "job-1",
                    "status": "completed",
                    "result_metadata": {"base": "b" * 40, "head": "c" * 40},
                },
                ("post", "/api/v1/projects/p1/comparison-comments"): COMPARISON_COMMENT,
            }
        )
        result = asyncio.run(
            tools_comments.add_comment(
                None,
                {
                    "project_id": "p1",
                    "content": "This change regresses the net",
                    "change": {
                        "jobId": "job-1",
                        "domain": "schematic",
                        "semanticItemId": "grp:net",
                    },
                },
                "Bearer t",
                routes,
            )
        )
        self.assertFalse(result["isError"])
        self.assertEqual(routes.calls[0][:2], ("get", "/api/v1/jobs/job-1"))
        method, path, body = routes.calls[1]
        self.assertEqual((method, path), ("post", "/api/v1/projects/p1/comparison-comments"))
        self.assertEqual(
            body,
            {
                "baseCommit": "b" * 40,
                "compareCommit": "c" * 40,
                "domain": "SCH",
                "content": "This change regresses the net",
                "semanticItemId": "grp:net",
                "anchorKind": "item",
            },
        )
        link = result["structuredContent"]["link"]
        self.assertIn("section=history", link)
        self.assertIn("comment=c2", link)

    def test_a_comparison_change_can_carry_its_commit_pair(self) -> None:
        routes = FakeRoutes(
            {("post", "/api/v1/projects/p1/comparison-comments"): COMPARISON_COMMENT}
        )
        asyncio.run(
            tools_comments.add_comment(
                None,
                {
                    "project_id": "p1",
                    "content": "note",
                    "change": {
                        "baseCommit": "b" * 40,
                        "compareCommit": "c" * 40,
                        "domain": "pcb",
                        "semanticItemId": "grp:1",
                    },
                },
                "Bearer t",
                routes,
            )
        )
        self.assertEqual(len(routes.calls), 1)
        self.assertEqual(routes.calls[0][2]["domain"], "PCB")

    def test_a_change_and_a_design_anchor_together_are_refused(self) -> None:
        routes = canvas_routes()
        result = asyncio.run(
            tools_comments.add_comment(
                None,
                {
                    "project_id": "p1",
                    "content": "note",
                    "component": "R1",
                    "change": {"domain": "pcb", "semanticItemId": "grp:1"},
                },
                "Bearer t",
                routes,
            )
        )
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "invalid-arguments")
        self.assertEqual(routes.calls, [])

    def test_a_change_without_a_job_or_commit_pair_is_refused(self) -> None:
        routes = canvas_routes()
        result = asyncio.run(
            tools_comments.add_comment(
                None,
                {
                    "project_id": "p1",
                    "content": "note",
                    "change": {"domain": "pcb", "semanticItemId": "grp:1"},
                },
                "Bearer t",
                routes,
            )
        )
        self.assertEqual(result["structuredContent"]["error"]["code"], "invalid-arguments")
        self.assertEqual(routes.calls, [])

    def test_an_unfinished_comparison_job_is_not_ready(self) -> None:
        routes = FakeRoutes(
            {
                ("get", "/api/v1/jobs/job-1"): {"job_id": "job-1", "status": "running"},
            }
        )
        result = asyncio.run(
            tools_comments.add_comment(
                None,
                {
                    "project_id": "p1",
                    "content": "note",
                    "change": {"jobId": "job-1", "domain": "pcb", "semanticItemId": "grp:1"},
                },
                "Bearer t",
                routes,
            )
        )
        self.assertTrue(result["isError"])
        error = result["structuredContent"]["error"]
        self.assertEqual((error["code"], error["status"]), ("comparison-not-ready", 409))


class ReplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch.object(settings, "PUBLIC_BASE_URL", "https://prism.test"))

    def test_a_reply_posts_the_content_and_cites_the_thread(self) -> None:
        routes = FakeRoutes(
            {
                ("post", "/api/v1/projects/p1/comments/c1/replies"): {
                    "comment": COMMENT,
                    "reply": {"id": "r1", "content": "Fixed"},
                }
            }
        )
        result = asyncio.run(
            tools_comments.reply_to_comment(
                None,
                {"project_id": "p1", "comment_id": "c1", "content": "Fixed"},
                "Bearer t",
                routes,
            )
        )
        self.assertFalse(result["isError"])
        self.assertEqual(routes.calls[0][2], {"content": "Fixed"})
        structured = result["structuredContent"]
        self.assertEqual(structured["reply"]["id"], "r1")
        self.assertIn("comment=c1", structured["link"])


class InvokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch.object(settings, "PUBLIC_BASE_URL", "https://prism.test"))

    def test_the_idempotency_key_travels_to_the_anchored_route(self) -> None:
        class StubApp:
            def __init__(self) -> None:
                self.headers: dict = {}

            async def __call__(self, scope, receive, send) -> None:
                await receive()
                self.headers = dict(scope["headers"])
                await send(
                    {"type": "http.response.start", "status": 201, "headers": []}
                )
                await send(
                    {"type": "http.response.body", "body": json.dumps(COMMENT).encode()}
                )

        app = StubApp()
        tool = registry.tool_named("add_comment")
        assert tool is not None
        result = asyncio.run(
            invoke.call_tool(
                app,
                tool,
                {"project_id": "p1", "content": "note", "component": "R1", "idempotencyKey": "k-1"},
                authorization="Bearer t",
            )
        )
        self.assertFalse(result["isError"])
        self.assertEqual(app.headers[b"idempotency-key"], b"k-1")


def caller() -> AuthenticatedUser:
    return AuthenticatedUser(
        email="agent@example.test",
        name="Agent",
        role="designer",
        auth_type=policy.MCP_AUTH_TYPE,
        client_id="https://client.example/meta.json",
        scopes=["api:read", "comments:write"],
    )


class ResolveHandoffTests(unittest.TestCase):
    def test_the_server_answers_resolve_with_a_url_elicitation(self) -> None:
        class Request:
            app = None

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "resolve_comment", "arguments": {"project_id": "p1", "comment_id": "c1"}},
        }
        with mock.patch.object(settings, "PUBLIC_BASE_URL", "https://prism.test"), \
                mock.patch.object(governance, "current", return_value=governance.McpGovernance()), \
                mock.patch.object(governance, "project_for_arguments", new=mock.AsyncMock(return_value="p1")), \
                mock.patch.object(audit, "record_quietly") as record:
            response = asyncio.run(mcp_api._handle(Request(), payload, "Bearer t", caller()))
        body = json.loads(response.body)
        self.assertEqual(body["error"]["code"], mcp_api.URL_ELICITATION_REQUIRED)
        elicitation = body["error"]["data"]["elicitations"][0]
        self.assertEqual(elicitation["mode"], "url")
        self.assertEqual(
            elicitation["url"], "https://prism.test/project/p1?section=visualizers&comment=c1"
        )
        self.assertEqual(elicitation["message"], tools_comments._RESOLVE_MESSAGE)
        self.assertEqual(record.call_args.kwargs["outcome"], "refused")
        self.assertEqual(record.call_args.kwargs["code"], "handoff")


if __name__ == "__main__":
    unittest.main()
