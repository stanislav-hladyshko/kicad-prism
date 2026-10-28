"""Route latency, the request log and slow queries under ``/api/ops`` (ADR §11, OPS-04b).

These are the read-only diagnostics behind ``get_route_latency``,
``list_recent_requests``, ``trace_request`` and ``get_slow_queries``. Each
route needs ``PRISM_MCP_OPS_ENABLED`` (otherwise 404) and ``ops_read``: an
admin, and for a bearer token the ``ops:read`` scope. The queries run in a
worker thread; results carry identifiers, timings and counts, never design
content, and every free-text field is redacted by the service that reads it.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.errors import NotFound
from app.core.security import AuthenticatedUser, require_ops_reader
from app.services.ops import latency, queries, trace

router = APIRouter(prefix="/api/ops", tags=["ops"])


class LatencyNumbers(BaseModel):
    count: int
    status4xx: int
    status5xx: int
    #: Share of requests in the window answered 4xx or 5xx, as a fraction.
    rate4xx: float
    rate5xx: float
    #: Estimated from the merged histogram; null when the quantile is in overflow.
    p50Ms: float | None
    p95Ms: float | None
    p99Ms: float | None
    overflow: bool
    #: Count weighted by bucket midpoints, in milliseconds.
    estimatedTotalMs: float


class RouteLatencyRow(LatencyNumbers):
    method: str
    route: str


class LatencyTotals(LatencyNumbers):
    routeCount: int


class RouteLatencyOut(BaseModel):
    windowMinutes: int
    totals: LatencyTotals
    routes: list[RouteLatencyRow]


class RecentRequestOut(BaseModel):
    requestId: str
    at: str | None
    method: str
    route: str
    status: int
    durationMs: int
    dbMs: int
    authType: str
    clientId: str
    #: True for an in-process MCP sub-call made inside another request.
    nested: bool


class TracedRequestOut(BaseModel):
    at: str | None
    method: str
    route: str
    status: int
    durationMs: int
    dbMs: int
    authType: str
    clientId: str
    nested: bool


class TracedJobOut(BaseModel):
    id: str
    kind: str
    status: str
    createdAt: str | None
    updatedAt: str | None
    startedAt: str | None
    completedAt: str | None


class TracedEventOut(BaseModel):
    id: int
    kind: str
    projectId: str | None
    topic: str
    createdAt: str | None


class TracedAuditOut(BaseModel):
    tool: str
    outcome: str
    code: str | None
    durationMs: int


class RequestTraceOut(BaseModel):
    requestId: str
    requests: list[TracedRequestOut]
    jobs: list[TracedJobOut]
    events: list[TracedEventOut]
    audit: list[TracedAuditOut]


class SlowQueryOut(BaseModel):
    query: str
    calls: int
    totalMs: float
    meanMs: float
    maxMs: float
    rows: int
    sharedBlksHit: int
    sharedBlksRead: int


class SlowQueriesOut(BaseModel):
    available: bool
    reason: str | None = None
    order: str
    queries: list[SlowQueryOut]


@router.get("/route-latency", response_model=RouteLatencyOut, summary="Route latency over a window")
async def get_route_latency(
    window_minutes: int = Query(default=latency.DEFAULT_WINDOW_MINUTES, ge=1, le=latency.MAX_WINDOW_MINUTES),
    route_prefix: str | None = Query(default=None, max_length=300),
    limit: int = Query(default=latency.DEFAULT_LIMIT, ge=1, le=latency.MAX_LIMIT),
    _user: AuthenticatedUser = Depends(require_ops_reader),
) -> RouteLatencyOut:
    result = await asyncio.to_thread(latency.route_latency, window_minutes, route_prefix, limit)
    return RouteLatencyOut(**result)


@router.get("/requests", response_model=list[RecentRequestOut], summary="Recent slow, write and failed requests")
async def list_recent_requests(
    limit: int = Query(default=trace.DEFAULT_LIMIT, ge=1, le=trace.MAX_LIMIT),
    min_duration_ms: int | None = Query(default=None, ge=0),
    status_min: int | None = Query(default=None, ge=100, le=599),
    route_prefix: str | None = Query(default=None, max_length=300),
    _user: AuthenticatedUser = Depends(require_ops_reader),
) -> list[RecentRequestOut]:
    rows = await asyncio.to_thread(
        trace.recent_requests, limit, min_duration_ms, status_min, route_prefix
    )
    return [RecentRequestOut(**row) for row in rows]


@router.get("/requests/{request_id}", response_model=RequestTraceOut, summary="One request id across jobs, events and audit")
async def trace_request(
    request_id: str,
    _user: AuthenticatedUser = Depends(require_ops_reader),
) -> RequestTraceOut:
    result = await asyncio.to_thread(trace.trace_request, request_id)
    if result is None:
        raise NotFound("No diagnostics carry that request id", code="request-not-found")
    return RequestTraceOut(**result)


@router.get("/slow-queries", response_model=SlowQueriesOut, summary="Slow statements from pg_stat_statements")
async def get_slow_queries(
    order: Literal["total", "mean"] = Query(default="total"),
    limit: int = Query(default=queries.DEFAULT_LIMIT, ge=1, le=queries.MAX_LIMIT),
    _user: AuthenticatedUser = Depends(require_ops_reader),
) -> SlowQueriesOut:
    result = await asyncio.to_thread(queries.slow_queries, order, limit)
    return SlowQueriesOut(**result)


__all__ = ["router"]
