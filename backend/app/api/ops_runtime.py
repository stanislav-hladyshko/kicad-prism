"""Runtime diagnostics under ``/api/ops`` (ADR §11, OPS-04c).

Three admin reads: this process's pool beside every worker's heartbeat, slot
usage and queue depth; cache hit rates and sizes; and event-log and webhook
health. Each answer is typed, carries identifiers, counts, sizes and timings
only, and the services redact free text before it reaches this module.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.core.security import AuthenticatedUser, require_ops_reader
from app.services.ops import caches, event_health, runtime

router = APIRouter(prefix="/api/ops", tags=["ops"])


class ProcessInfo(BaseModel):
    host: str
    pid: int
    kind: str


class WorkerInfo(BaseModel):
    worker_id: str
    pool: str
    host: str | None = None
    pid: int | None = None
    started_at: str | None = None
    concurrency: int | None = None
    memory_budget_mib: int | None = None
    running_jobs: list[dict[str, Any]] = Field(default_factory=list)
    last_seen_at: str | None = None
    stale: bool


class ResourceUsage(BaseModel):
    resource: str
    capacity: int
    in_use: int
    free: int


class QueueDepth(BaseModel):
    worker_pool: str
    kind: str
    status: str
    count: int


class OldestQueued(BaseModel):
    worker_pool: str
    oldest_queued_at: str | None = None
    age_seconds: float


class PoolAndWorkers(BaseModel):
    process: ProcessInfo
    pool: dict[str, Any]
    workers: list[WorkerInfo]
    resources: list[ResourceUsage]
    queue: list[QueueDepth]
    oldest_queued: list[OldestQueued]
    memory_budget_mib: int


class CacheEntry(BaseModel):
    name: str
    hits: int | None = None
    misses: int | None = None
    hit_rate: float | None = None
    table_rows: int | None = None
    table_bytes: int | None = None
    oldest_access: str | None = None
    newest_access: str | None = None
    disk_bytes: int | None = None
    disk_files: int | None = None
    disk_partial: bool = False


class BundleStore(BaseModel):
    archives: int
    archive_bytes: int


class CacheStats(BaseModel):
    window_minutes: int
    caches: list[CacheEntry]
    bundle_store: BundleStore


class EventKindCount(BaseModel):
    kind: str
    count: int


class DeliveryHealth(BaseModel):
    deliveries: int
    ok: int
    failed: int
    error_rate: float


class WebhookHealth(BaseModel):
    id: str
    host: str
    status: str
    event_kinds: list[str]
    cursor: int
    lag: int
    failing_since: str | None = None
    attempts: int
    next_attempt_at: str | None = None
    last_status: int | None = None
    last_error: str | None = None
    deliveries_24h: DeliveryHealth


class EventAndWebhookHealth(BaseModel):
    head_cursor: int
    oldest_retained_at: str | None = None
    retention_days: int
    events_last_hour: list[EventKindCount]
    webhooks: list[WebhookHealth]


@router.get(
    "/pool-and-workers",
    response_model=PoolAndWorkers,
    summary="This process's pool, live workers, resource slots and queue depth",
)
async def pool_and_workers(
    user: AuthenticatedUser = Depends(require_ops_reader),
) -> PoolAndWorkers:
    return PoolAndWorkers(**await asyncio.to_thread(runtime.pool_and_workers))


@router.get(
    "/caches",
    response_model=CacheStats,
    summary="Cache hit rates, table sizes and bounded on-disk sizes",
)
async def cache_stats(
    window_minutes: int = Query(default=60, ge=1, le=7 * 24 * 60),
    user: AuthenticatedUser = Depends(require_ops_reader),
) -> CacheStats:
    return CacheStats(**await asyncio.to_thread(caches.cache_stats, window_minutes))


@router.get(
    "/event-health",
    response_model=EventAndWebhookHealth,
    summary="Event-log head, retention and webhook lag and delivery health",
)
async def event_and_webhook_health(
    user: AuthenticatedUser = Depends(require_ops_reader),
) -> EventAndWebhookHealth:
    return EventAndWebhookHealth(**await asyncio.to_thread(event_health.event_and_webhook_health))


__all__ = ["router"]
