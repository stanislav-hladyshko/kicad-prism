"""The versioned API (ADR scripting-and-mcp §3.2).

Every route is written once, under ``/api``. ``/api/v1/...`` reaches the same route:
``ApiVersionMiddleware`` rewrites the path before routing, so there is one route table
and one set of handlers. The published OpenAPI document names the ``/api/v1`` paths
only. The unversioned paths stay as aliases for the browser and existing tokens until
v6.

An operation's id is its route function's name. It is part of the contract:
``app.core.api_contract`` records every id, and renaming one is a breaking change.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.types import ASGIApp, Receive, Scope, Send

API_PREFIX = "/api"
V1_PREFIX = "/api/v1"
_V1_RAW = V1_PREFIX.encode()


def operation_id(route: APIRoute) -> str:
    """FastAPI's ``generate_unique_id_function``: the route function's name."""
    return route.name


def _versioned(path: str) -> str | None:
    if path == V1_PREFIX or path.startswith(V1_PREFIX + "/"):
        return API_PREFIX + path[len(V1_PREFIX):]
    return None


class ApiVersionMiddleware:
    """Serves ``/api/v1/...`` from the ``/api/...`` route table."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            path = _versioned(scope["path"])
            if path is not None:
                scope = dict(scope)
                scope["path"] = path
                raw = scope.get("raw_path")
                if raw is not None and raw.startswith(_V1_RAW):
                    scope["raw_path"] = API_PREFIX.encode() + raw[len(_V1_RAW):]
                scope.setdefault("state", {})["api_version"] = "v1"
        await self.app(scope, receive, send)


def install_versioned_openapi(app: FastAPI) -> None:
    """Publish the ``/api`` routes under ``/api/v1``, and nothing outside ``/api``."""
    build = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = build()
        schema["paths"] = {
            V1_PREFIX + path[len(API_PREFIX):]: operations
            for path, operations in schema["paths"].items()
            if path.startswith(API_PREFIX + "/")
        }
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
