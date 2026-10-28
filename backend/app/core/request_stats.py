"""Request ids, per-route timing and the request log (ADR scripting-and-mcp §11, D23–D24).

The outermost middleware. For every HTTP request it:

- takes a well-formed inbound ``X-Request-Id`` or makes one, keeps it in the
  request context (jobs, events and audit rows the request writes pick it up)
  and returns it in the response header. A request MCP makes in-process runs
  inside the ``tools/call`` that caused it, so it keeps that id;
- times it and adds it to this minute's statistics for its route template;
- logs a row in ``ws_ops_requests`` for every write, every request slower than
  1 s and every 5xx, with the time spent holding database connections.

None of this touches the database on the request path (``services.ops.stats``
buffers it), and a failure here never fails the request.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import request_context
from app.core.security import AUTHENTICATED_USER_STATE
from app.core.token_audit import route_template
from app.services.ops import stats

logger = logging.getLogger(__name__)

SLOW_MS = 1000
_READS = frozenset({"GET", "HEAD", "OPTIONS"})
_HEADER = request_context.HEADER.encode("latin-1")


def _inbound_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers") or []:
        if name.lower() == _HEADER:
            return value.decode("latin-1")
    return None


class RequestStatsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        outer = request_context.current()
        if outer is not None:
            context, token = outer, None
        else:
            context, token = request_context.begin(_inbound_id(scope))
        started = time.perf_counter()
        status: int | None = None
        header_value = context.request_id.encode("latin-1")

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != _HEADER]
                headers.append((_HEADER, header_value))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            try:
                self._record(scope, context, status, started)
            except Exception:  # noqa: BLE001 - statistics never fail a request
                logger.debug("Could not record request statistics", exc_info=True)
            if token is not None:
                request_context.end(token)

    @staticmethod
    def _record(scope: Scope, context: request_context.RequestContext, status: int | None, started: float) -> None:
        duration_ms = (time.perf_counter() - started) * 1000
        method = str(scope.get("method", "")).upper()
        route = route_template(scope) or "<unmatched>"
        code = status if status is not None else 500
        stats.record_request(method, route, code, duration_ms)
        if method in _READS and duration_ms < SLOW_MS and code < 500:
            return
        user = (scope.get("state") or {}).get(AUTHENTICATED_USER_STATE)
        stats.log_request(
            {
                "request_id": context.request_id,
                "at": datetime.now(timezone.utc),
                "method": method,
                "route": route,
                "status": code,
                "duration_ms": int(duration_ms),
                "db_ms": int(context.db_seconds * 1000),
                "auth_type": str(getattr(user, "auth_type", "") or ""),
                "user_email": str(getattr(user, "email", "") or ""),
                "client_id": str(getattr(user, "client_id", "") or ""),
                "details": {"nested": scope.get("client") == ("mcp", 0)},
            }
        )


__all__ = ["RequestStatsMiddleware", "SLOW_MS"]
