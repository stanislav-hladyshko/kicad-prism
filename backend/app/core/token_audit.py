"""Record what a bearer token did with an HTTP write (ADR §10 D19).

The MCP audit already records ``tools/call`` and ``resources/read``. This
middleware adds the other half: a non-GET request under ``/api/`` authenticated
by a bearer token appends one ``method = 'http'`` row after its response is
sent. Browser sessions are never recorded, and the MCP server's in-process route
calls are skipped because their ``tools/call`` row already describes them.

The caller is not resolved a second time here. ``get_current_user`` leaves the
resolved :class:`AuthenticatedUser` in the ASGI scope's state while the auth
dependency runs, and this middleware reads it after the response. Authentication
therefore stays in one place, and the audit costs the request one dict lookup.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.security import AUTHENTICATED_USER_STATE
from app.services.mcp import audit

API_PATH_PREFIX = "/api/"
#: A read does not change anything, so only writes are activity worth recording.
SKIPPED_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: ``app.services.mcp.invoke`` builds its ASGI scope with this client address.
IN_PROCESS_MCP_CLIENT = ("mcp", 0)
SESSION_AUTH_TYPE = "session"


def applies(scope: Scope) -> bool:
    """Whether this request is one the audit describes."""

    return (
        scope.get("type") == "http"
        and str(scope.get("method", "")).upper() not in SKIPPED_METHODS
        and str(scope.get("path", "")).startswith(API_PATH_PREFIX)
        and scope.get("client") != IN_PROCESS_MCP_CLIENT
    )


def route_template(scope: Scope) -> str:
    """The matched route's template, never the concrete path with its ids.

    FastAPI 1.6 keeps an included router's prefix in its own route context and
    leaves ``scope["route"].path_format`` as the route's own suffix. The
    effective context holds the full template, so prefer it and fall back to the
    route itself for an app-level route that has no include context.
    """

    fastapi_scope = scope.get("fastapi")
    if isinstance(fastapi_scope, dict):
        context = fastapi_scope.get("effective_route_context")
        template = getattr(context, "path_format", None) or getattr(context, "path", None)
        if template:
            return str(template)
    route = scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    return str(template or "")


def row_for(scope: Scope, user: Any, status: int | None, started: float) -> dict[str, Any]:
    """One audit row describing a finished request."""

    status_ok = status is not None and 200 <= status < 400
    outcome = "ok" if status_ok else "refused" if status in (401, 403) else "error"
    path_params = scope.get("path_params") or {}
    project_id = path_params.get("project_id")
    return {
        "user_email": str(getattr(user, "email", "")),
        "auth_type": str(getattr(user, "auth_type", "")),
        "client_id": str(getattr(user, "client_id", "")),
        "token_id": str(getattr(user, "token_id", "")),
        "method": "http",
        "tool": f"{str(scope.get('method', '')).upper()} {route_template(scope)}",
        "project_id": str(project_id) if project_id else None,
        "revision": None,
        "outcome": outcome,
        # The status tells an administrator why a write did not succeed; a 2xx
        # needs no explanation, and its code column stays empty for it.
        "code": str(status) if status is not None and not 200 <= status < 300 else None,
        "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
    }


class TokenAuditMiddleware:
    """Append one audit row for a bearer token's completed HTTP write."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not applies(scope):
            await self.app(scope, receive, send)
            return

        started = time.monotonic()
        status: int | None = None

        async def capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            user = (scope.get("state") or {}).get(AUTHENTICATED_USER_STATE)
            # A bearer call is the one to record: session callers carry
            # ``auth_type == "session"``, and an unauthenticated request never
            # reaches a dependency that would leave a caller in the state.
            auth_type = getattr(user, "auth_type", SESSION_AUTH_TYPE) if user is not None else ""
            if auth_type and auth_type != SESSION_AUTH_TYPE:
                # A record, not part of the request: ``record_quietly`` logs and
                # swallows a failure, and the thread keeps the insert off the loop.
                await asyncio.to_thread(
                    audit.record_quietly, **row_for(scope, user, status, started)
                )


__all__ = ["TokenAuditMiddleware", "applies", "route_template", "row_for"]
