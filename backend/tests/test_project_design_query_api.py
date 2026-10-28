"""The design-query routes of /api/projects (ADR §3.3, §3.8).

``semantic_index_service.artifact_file`` is stubbed with the fixture member, so
no bundle is built; the project lookup and revision resolution are stubbed at
the route's seams. The tests pin the response shapes, the two 404s, the 400 for
an unknown revision, and the immutable cache header for a full sha.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import Response

from app.api import project_design_query as api
from app.api._helpers import PINNED_CACHE_CONTROL
from app.core.errors import BadRequest, NotFound
from app.services import semantic_index_bundle
from app.services.design_query import index as design_index

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "design_query" / "semantic_index.json"
COMMIT = "1234567890abcdef1234567890abcdef12345678"
FULL_SHA = "abcdef0123456789abcdef0123456789abcdef01"
REVISION_KEY = "b" * 64
GENERATOR = "c" * 64


class _User:
    def __init__(self, role: str = "viewer") -> None:
        self.email = "viewer@example.com"
        self.name = "Viewer"
        self.role = role


class _RouteHelpers:
    """The shared seams: a stubbed artifact, project lookup and revision resolver."""

    def setUp(self) -> None:
        design_index.clear_cache()
        self.addCleanup(design_index.clear_cache)
        self.project = SimpleNamespace(id="p1", path="/repo/project")
        self.artifact = semantic_index_bundle.ArtifactFile(FIXTURE, REVISION_KEY, GENERATOR)
        self.read = self.enterContext(
            mock.patch.object(
                design_index.semantic_index_service, "artifact_file", return_value=self.artifact
            )
        )
        self.enterContext(
            mock.patch.object(api, "get_project_for_role_or_404", return_value=self.project)
        )
        self.resolve = self.enterContext(
            mock.patch.object(api.design_revision, "resolve", return_value=COMMIT)
        )

    def _search(
        self,
        q: str,
        *,
        kind: list[str] | None = None,
        revision: str | None = None,
        limit: int = 50,
    ) -> tuple[dict, Response]:
        response = Response()
        payload = asyncio.run(
            api.search_design(
                "p1",
                response,
                q=q,
                kind=kind,
                revision=revision,
                limit=limit,
                user=_User(),
            )
        )
        api.DesignSearchOut(**payload)
        return payload, response

    def _component(self, ref: str, *, revision: str | None = None) -> tuple[dict, Response]:
        response = Response()
        payload = asyncio.run(
            api.get_design_component("p1", response, ref, revision=revision, user=_User())
        )
        api.DesignComponentOut(**payload)
        return payload, response

    def _net(self, name: str, *, revision: str | None = None) -> tuple[dict, Response]:
        response = Response()
        payload = asyncio.run(
            api.get_design_net("p1", response, name, revision=revision, user=_User())
        )
        api.DesignNetOut(**payload)
        return payload, response


class RouteTests(_RouteHelpers, unittest.TestCase):
    # ------------------------------------------------------------- search

    def test_search_names_the_commit_and_the_design_key(self) -> None:
        payload, response = self._search("u1")
        self.assertEqual(payload["revision"], {"commit": COMMIT, "designKey": REVISION_KEY})
        self.assertEqual(payload["query"], "u1")
        self.assertEqual(payload["results"][0]["reference"], "U1")
        self.assertEqual(response.headers["cache-control"], "private, no-cache")
        self.read.assert_called_with(self.project, COMMIT)

    def test_search_kind_filter_is_applied(self) -> None:
        payload, _ = self._search("u1", kind=["net"])
        self.assertEqual([hit["kind"] for hit in payload["results"]], ["net"])
        self.assertEqual(payload["results"][0]["name"], "U1/ADJ")

    def test_search_limit_is_applied(self) -> None:
        payload, _ = self._search("u1", kind=["component"], limit=1)
        self.assertEqual([hit["reference"] for hit in payload["results"]], ["U1"])

    # ---------------------------------------------------------- component

    def test_component_shape(self) -> None:
        payload, response = self._component("U1")
        component = payload["component"]
        self.assertEqual(payload["revision"]["commit"], COMMIT)
        self.assertEqual(payload["revision"]["designKey"], REVISION_KEY)
        self.assertEqual(component["value"], "LM317T")
        self.assertEqual(component["sheet"], "/Power/")
        self.assertEqual(component["fields"]["Manufacturer Part Number"], "LM317T")
        self.assertEqual(
            [(pin["pin"], pin["net"]) for pin in component["pins"]],
            [("1", "+12V"), ("2", "U1/ADJ"), ("3", "VOUT")],
        )
        self.assertEqual(response.headers["cache-control"], "private, no-cache")

    def test_an_unknown_component_is_404(self) -> None:
        with self.assertRaises(NotFound) as caught:
            self._component("X99")
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(caught.exception.code, "component-not-found")

    # ---------------------------------------------------------------- net

    def test_net_shape_including_a_slash_in_the_name(self) -> None:
        payload, response = self._net("Power/+12V")
        net = payload["net"]
        self.assertEqual(net["name"], "Power/+12V")
        self.assertEqual(net["components"], ["J3"])
        self.assertEqual(net["pins"], [{"reference": "J3", "pin": "1", "pinName": "Pin_1"}])
        self.assertEqual(response.headers["cache-control"], "private, no-cache")

    def test_an_unknown_net_is_404(self) -> None:
        with self.assertRaises(NotFound) as caught:
            self._net("NOPE")
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(caught.exception.code, "net-not-found")

    # ----------------------------------------------------------- revision

    def test_an_unknown_revision_is_400(self) -> None:
        self.resolve.side_effect = ValueError("Unknown revision: nope")
        with self.assertRaises(BadRequest) as caught:
            self._search("u1", revision="nope")
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.code, "revision-unknown")

    def test_a_full_sha_is_served_immutable(self) -> None:
        payload, response = self._search("u1", revision=FULL_SHA)
        self.assertEqual(payload["revision"]["commit"], COMMIT)
        self.assertEqual(response.headers["cache-control"], PINNED_CACHE_CONTROL)

    def test_a_branch_revalidates(self) -> None:
        _, response = self._search("u1", revision="main")
        self.assertEqual(response.headers["cache-control"], "private, no-cache")


class HttpRouteTests(_RouteHelpers, unittest.TestCase):
    """The same routes through FastAPI: query parsing, path conversion, error bodies."""

    def setUp(self) -> None:
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.core.errors import install_error_handlers
        from app.core.security import require_viewer

        app = FastAPI()
        install_error_handlers(app)
        app.include_router(api.router, prefix="/api/projects")
        app.dependency_overrides[require_viewer] = _User
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_kind_is_a_repeated_query_parameter(self) -> None:
        response = self.client.get(
            "/api/projects/p1/design/search", params={"q": "u1", "kind": ["component", "net"]}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([hit["kind"] for hit in body["results"]], ["component", "net"])
        self.assertEqual(body["revision"], {"commit": COMMIT, "designKey": REVISION_KEY})

    def test_a_net_path_keeps_its_slash(self) -> None:
        response = self.client.get("/api/projects/p1/design/nets/Power/+12V")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["net"]["name"], "Power/+12V")

    def test_a_full_sha_is_served_immutable(self) -> None:
        response = self.client.get(
            "/api/projects/p1/design/search", params={"q": "u1", "revision": FULL_SHA}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], PINNED_CACHE_CONTROL)

    def test_an_unknown_component_keeps_the_typed_error_body(self) -> None:
        response = self.client.get("/api/projects/p1/design/components/X99")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Component not found", "code": "component-not-found"})

    def test_an_unknown_revision_is_a_400_with_its_code(self) -> None:
        self.resolve.side_effect = ValueError("Unknown revision: nope")
        response = self.client.get(
            "/api/projects/p1/design/search", params={"q": "u1", "revision": "nope"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "Unknown revision", "code": "revision-unknown"})


if __name__ == "__main__":
    unittest.main()
