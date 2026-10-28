"""The ``render_view`` MCP tool's multi-step invocation (ADR D9).

The route is faked here: the start body, the polled record and the PNG bytes
are the only three things the tool asks the app for. The real app's POST and
GET routes are covered by the render integration test and the MCP server test.
"""

from __future__ import annotations

import asyncio
import base64
import json
import unittest
from unittest import mock

from app.core.config import settings
from app.services.mcp import invoke
from app.services.mcp import tools_render

RENDER_ID = "a" * 64
COMMIT = "b" * 40
PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"


def arguments() -> dict:
    return {
        "project_id": "p1",
        "view": "schematic",
        "highlight": {"components": ["R1"]},
        "width": 640,
    }


class FakeRoutes:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes | None]] = []

    async def __call__(self, app, method, path, query, authorization, body):
        self.calls.append((method, path, body))
        if method == "post":
            return 200, json.dumps(
                {"renderId": RENDER_ID, "jobId": "job-1", "status": "queued"}
            ).encode()
        if path.endswith(".png"):
            return 200, PNG
        return 200, json.dumps(
            {
                "renderId": RENDER_ID,
                "status": "completed",
                "view": "schematic",
                "commit": COMMIT,
                "sheet": "1",
                "region": {"x": 14, "y": 14, "width": 12, "height": 12},
                "width": 640,
                "height": 640,
                "highlights": [{"kind": "component", "key": "R1", "found": True}],
                "pngUrl": f"/api/v1/projects/p1/renders/{RENDER_ID}.png",
            }
        ).encode()


class ImageResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch.object(settings, "PUBLIC_BASE_URL", "https://prism.test"))

    def test_a_finished_render_returns_an_image_block(self) -> None:
        routes = FakeRoutes()
        result = asyncio.run(
            tools_render.execute(None, arguments(), "Bearer token", routes)
        )
        self.assertFalse(result["isError"])
        self.assertEqual(result["content"][0]["type"], "image")
        self.assertEqual(result["content"][0]["mimeType"], "image/png")
        self.assertEqual(base64.b64decode(result["content"][0]["data"]), PNG)
        self.assertEqual(result["content"][1]["type"], "text")
        structured = result["structuredContent"]
        self.assertEqual(structured["renderId"], RENDER_ID)
        self.assertEqual(
            structured["link"],
            "https://prism.test/project/p1?section=visualizers&tab=sch"
            f"&commit={COMMIT}&sel=R1",
        )
        self.assertEqual(
            structured["uri"], f"prism://project/p1/rev/{COMMIT}/render/{RENDER_ID}"
        )
        self.assertEqual(routes.calls[0][2], tools_render._body(arguments()))


class LinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch.object(settings, "PUBLIC_BASE_URL", "https://prism.test"))

    def test_found_highlights_become_the_selection_and_net_highlight(self) -> None:
        link = tools_render._link(
            "p1",
            COMMIT,
            "pcb",
            [
                {"kind": "component", "key": "R1", "found": True},
                {"kind": "net", "key": "Power/+12V", "found": True},
                {"kind": "component", "key": "R99", "found": False},
            ],
        )
        self.assertEqual(
            link,
            "https://prism.test/project/p1?section=visualizers&tab=pcb"
            f"&commit={COMMIT}&sel=R1&nets=Power%252F%252B12V",
        )


class PendingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch.object(tools_render, "POLL_TIMEOUT_SECONDS", 0.0))
        self.enterContext(mock.patch.object(tools_render, "POLL_INTERVAL_SECONDS", 0.0))

    def test_a_running_render_is_not_an_error(self) -> None:
        async def routes(app, method, path, query, authorization, body):
            if method == "post":
                return 200, json.dumps({"renderId": RENDER_ID, "jobId": "j", "status": "queued"}).encode()
            return 200, json.dumps({"renderId": RENDER_ID, "status": "running"}).encode()

        result = asyncio.run(tools_render.execute(None, arguments(), "Bearer token", routes))
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"], {"renderId": RENDER_ID, "status": "running"})
        self.assertIn("again", result["content"][0]["text"])
        self.assertEqual(result["content"][0]["type"], "text")


class ErrorTests(unittest.TestCase):
    def test_a_hidden_project_is_a_not_found_execution_error(self) -> None:
        async def routes(app, method, path, query, authorization, body):
            return 404, json.dumps({"detail": "Project not found", "code": "not_found"}).encode()

        result = asyncio.run(tools_render.execute(None, arguments(), "Bearer token", routes))
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["status"], 404)
        self.assertIn("not_found", result["content"][0]["text"])


class ServerContentTests(unittest.TestCase):
    def test_the_server_keeps_an_image_content_block(self) -> None:
        from app.api import mcp as mcp_api

        image_result = {
            "content": [
                {"type": "image", "data": base64.b64encode(PNG).decode("ascii"), "mimeType": "image/png"},
                {"type": "text", "text": "ready"},
            ],
            "structuredContent": {"renderId": RENDER_ID, "status": "completed"},
            "isError": False,
            "_meta": dict(invoke.UNTRUSTED_META),
        }

        class Request:
            app = None

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "render_view", "arguments": arguments()},
        }
        from app.core.security import AuthenticatedUser
        from app.services.mcp import audit, governance

        user = AuthenticatedUser(email="agent@example.test", name="Agent", role="viewer", auth_type="pat", scopes=["api:read"])
        with mock.patch.object(invoke, "call_tool", new=mock.AsyncMock(return_value=image_result)), \
                mock.patch.object(governance, "current", return_value=governance.McpGovernance()), \
                mock.patch.object(audit, "record_quietly"):
            response = asyncio.run(mcp_api._handle(Request(), payload, "Bearer token", user))
        body = json.loads(response.body)
        self.assertEqual(body["result"]["content"][0]["type"], "image")
        self.assertEqual(body["result"]["content"][1]["text"], "ready")


if __name__ == "__main__":
    unittest.main()
