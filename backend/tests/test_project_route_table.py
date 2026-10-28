"""The /api/projects route table survives S1's router split unchanged.

S1 moves routes out of app/api/projects.py into scoped routers (ownership.md, S1). Two
snapshots prove that changed nothing a client can see:

- dispatch: for every HTTP method and every route's sample URL under /api/projects, the
  endpoint that wins first match, so a moved route can neither shadow nor be shadowed;
- operations: a digest of each OpenAPI operation (parameters, bodies, responses, tags,
  operation id, summary), so a moved route keeps its contract.

Regenerate only for an intended route change:
    python -m tests.test_project_route_table --write
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

SNAPSHOT = Path(__file__).parent / "fixtures" / "project_route_table.json"
PREFIX = "/api/projects"
METHODS = ("DELETE", "GET", "PATCH", "POST", "PUT")


def _app():
    from app.core.config import settings

    with patch.dict(os.environ, {"AUTH_ENABLED": "false"}), patch.object(settings, "AUTH_ENABLED_OVERRIDE", False):
        import app.main as main
    return main.app


def _flat(routes, prefix: str = ""):
    """Every APIRoute in matching order, with its full path (FastAPI keeps includes lazy)."""
    from fastapi.routing import APIRoute

    for route in routes:
        context = getattr(route, "include_context", None)
        if context is not None:
            yield from _flat(route.original_router.routes, prefix + context.prefix)
        elif isinstance(route, APIRoute):
            yield prefix + route.path, route


def _sample(path: str) -> str:
    path = re.sub(r"\{[^}:]+:path\}", "seg1/seg2", path)
    return re.sub(r"\{[^}]+\}", "p0", path)


def current_table() -> dict:
    from starlette.routing import compile_path

    app = _app()
    table = [(path, route, compile_path(path)[0]) for path, route in _flat(app.router.routes)]

    def winner(method: str, url: str) -> str | None:
        for path, route, regex in table:
            if method in route.methods and regex.match(url):
                return f"{route.endpoint.__name__} {path}"
        return None

    samples = sorted({_sample(path) for path, _, _ in table if path.startswith(PREFIX)})
    dispatch = {}
    for url in samples:
        for method in METHODS:
            hit = winner(method, url)
            if hit is not None:
                dispatch[f"{method} {url}"] = hit
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        # The published document renames the /api paths to /api/v1; undo that so
        # the snapshot stays keyed by the route table this test guards.
        paths = {
            "/api" + path[len("/api/v1"):]: item
            for path, item in app.openapi()["paths"].items()
            if path.startswith("/api/v1/")
        }
    operations = {
        f"{method.upper()} {path}": hashlib.sha256(json.dumps(operation, sort_keys=True).encode()).hexdigest()
        for path, item in paths.items()
        if path.startswith(PREFIX)
        for method, operation in item.items()
    }
    return {"dispatch": dispatch, "operations": operations}


class ProjectRouteTableTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        cls.actual = current_table()

    def test_every_url_dispatches_to_the_same_endpoint(self) -> None:
        self.assertEqual(self.actual["dispatch"], self.expected["dispatch"])

    def test_every_operation_keeps_its_contract(self) -> None:
        self.assertEqual(self.actual["operations"], self.expected["operations"])


if __name__ == "__main__":
    if sys.argv[1:] == ["--write"]:
        SNAPSHOT.write_text(json.dumps(current_table(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {SNAPSHOT}")
    else:
        unittest.main()
