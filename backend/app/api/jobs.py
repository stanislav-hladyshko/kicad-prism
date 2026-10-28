from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from app.api._helpers import authorize_job_read
from app.core import policy
from app.core.security import AuthenticatedUser, require_admin, require_viewer
from app.services.job_artifact_service import job_artifacts
from app.services.job_runtime import JobLogNotFound, tail_job_log
from app.services.job_service import jobs
from app.services.postgres_database import database
from app.services.comment_live_broker import broker as comment_live_broker


router = APIRouter(dependencies=[Depends(require_viewer)])


class ClaimLatency(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    p50: float
    p95: float
    max: float


class JobUpdateRate(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    p95: float
    max: float


class KindStatusCount(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    kind: str
    status: str
    count: int


class DatabasePoolMetrics(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    connectionWaitCount: int
    connectionWaitMeanMs: float
    connectionWaitMaxMs: float
    pool: dict[str, Any]


class CommentLiveMetrics(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    connections: int
    listenerConnected: bool
    listenerReconnects: int
    replayFailures: int
    deliveredEvents: int
    eventLagMeanMs: float
    eventLagMaxMs: float


class ThreadPoolMetrics(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    borrowedTokens: int
    totalTokens: int
    queueDepth: int


class BenchmarkMetrics(BaseModel):
    """Queue latency, job write rate and the API's pool counters."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    since: str
    jobCount: int
    activeJobs: int
    claimLatencyMs: ClaimLatency
    jobUpdateRatePerSecond: JobUpdateRate
    byKindStatus: list[KindStatusCount]
    apiDatabasePool: DatabasePoolMetrics
    apiCommentLive: CommentLiveMetrics
    apiThreadPool: ThreadPoolMetrics


class JobStatus(BaseModel):
    """The slim job row the status route returns."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    job_id: str
    kind: Optional[str]
    status: Optional[str]
    stage: Optional[str]
    message: Optional[str]
    percent: Optional[float]
    attempt: Optional[int]
    max_attempts: Optional[int]
    project_id: Optional[str]
    repository_id: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]
    available_at: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]
    cancel_requested_at: Optional[str]
    result_digest: Optional[str]
    result_metadata: Optional[dict[str, Any]]
    error_code: Optional[str]
    error_message: Optional[str]
    result_url: Optional[str]
    # Only present when the job's payload or result names a pipeline.
    pipeline: Optional[Any] = None


class JobEvent(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: int
    event_type: str
    stage: Optional[str]
    percent: Optional[float]
    details: dict[str, Any]
    created_at: str


class JobEvents(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    job_id: str
    events: list[JobEvent]


class JobLogs(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    job_id: str
    lines: list[str]


class JobCancelRequested(BaseModel):
    """A cancel request: the status the service reports may be a terminal one."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    job_id: str
    status: Optional[str]


def _authorize_cancel(job: dict, user: AuthenticatedUser) -> None:
    authorize_job_read(job, user)
    policy.authorize(user, "catalog_write" if job.get("worker_pool") == "catalog" else "design")


def _slim_status(job: dict) -> dict:
    status = {
        key: job.get(key)
        for key in (
            "job_id",
            "kind",
            "status",
            "stage",
            "message",
            "percent",
            "attempt",
            "max_attempts",
            "project_id",
            "repository_id",
            "created_at",
            "updated_at",
            "available_at",
            "started_at",
            "completed_at",
            "cancel_requested_at",
            "result_digest",
            "result_metadata",
            "error_code",
            "error_message",
        )
    }
    payload = job.get("payload") or {}
    if isinstance(payload, dict) and payload.get("pipeline") is not None:
        status["pipeline"] = payload["pipeline"]
    elif isinstance(job.get("result_metadata"), dict):
        pipeline = job["result_metadata"].get("pipeline")
        if pipeline is not None:
            status["pipeline"] = pipeline
    status["result_url"] = (
        f"/api/jobs/{job['job_id']}/artifact"
        if job.get("status") == "completed" and job.get("result_digest")
        else None
    )
    return status


def _get_authorized_job(job_id: str, user: AuthenticatedUser) -> dict:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    authorize_job_read(job, user)
    return job


@router.get("/benchmark-metrics", response_model=BenchmarkMetrics, summary="Queue latency and job write-rate metrics")
async def benchmark_metrics(
    since: str = Query(default=""),
    user: AuthenticatedUser = Depends(require_admin),
):
    try:
        since_at = (
            datetime.fromisoformat(since.replace("Z", "+00:00"))
            if since
            else datetime.now(timezone.utc) - timedelta(minutes=15)
        )
        if since_at.tzinfo is None:
            since_at = since_at.replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid since timestamp") from error
    snapshot = await asyncio.to_thread(jobs.benchmark_snapshot, since=since_at)
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter_stats = limiter.statistics()
    snapshot["apiDatabasePool"] = database.metrics_snapshot()
    snapshot["apiCommentLive"] = comment_live_broker.metrics_snapshot()
    snapshot["apiThreadPool"] = {
        "borrowedTokens": int(limiter.borrowed_tokens),
        "totalTokens": int(limiter.total_tokens),
        "queueDepth": int(limiter_stats.tasks_waiting),
    }
    return snapshot


@router.get("/{job_id}", response_model=JobStatus, response_model_exclude_unset=True, summary="One job's status")
async def get_job(
    job_id: str,
    user: AuthenticatedUser = Depends(require_viewer),
):
    job = await asyncio.to_thread(jobs.get, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    authorize_job_read(job, user)
    return _slim_status(job)


@router.post(
    "/{job_id}/cancel",
    status_code=202,
    response_model=JobCancelRequested,
    summary="Ask a job to cancel",
)
async def cancel_job(
    job_id: str,
    user: AuthenticatedUser = Depends(require_viewer),
):
    job = await asyncio.to_thread(jobs.get, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    _authorize_cancel(job, user)
    status = await asyncio.to_thread(
        jobs.request_cancel,
        job_id,
        requested_by=user.email,
    )
    return {"job_id": job_id, "status": status}


@router.get("/{job_id}/events", response_model=JobEvents, summary="A job's progress events after a given id")
async def get_job_events(
    job_id: str,
    after: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    user: AuthenticatedUser = Depends(require_viewer),
):
    job = await asyncio.to_thread(jobs.get, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    authorize_job_read(job, user)
    events = await asyncio.to_thread(jobs.events, job_id, after=after, limit=limit)
    return {"job_id": job_id, "events": events}


@router.get(
    "/{job_id}/artifact",
    responses={200: {"description": "The job's artifact file.", "content": {"*/*": {}}}},
    summary="Download a completed job's artifact",
)
async def get_job_artifact(
    job_id: str,
    user: AuthenticatedUser = Depends(require_viewer),
):
    job = await asyncio.to_thread(jobs.get, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    authorize_job_read(job, user)
    artifact = await asyncio.to_thread(jobs.get_artifact_for_job, job_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Job artifact not found")
    path = job_artifacts.store.contains(str(artifact.get("object_path") or ""))
    if path is None:
        raise HTTPException(status_code=404, detail="Job artifact not found")
    if not path.is_file():
        await asyncio.to_thread(
            jobs.invalidate_artifact,
            str(artifact["id"]),
            reason="object_missing_on_download",
        )
        raise HTTPException(status_code=404, detail="Job artifact not found")
    digest = str(artifact.get("digest") or "")
    return FileResponse(
        path,
        media_type=str(artifact.get("media_type") or "application/octet-stream"),
        headers={
            "ETag": f'"{digest}"',
            "Cache-Control": "private, max-age=31536000, immutable",
        },
    )


def _tail_log(path: Path, line_count: int) -> list[str]:
    try:
        return tail_job_log(path, line_count)
    except JobLogNotFound as error:
        raise HTTPException(status_code=404, detail="Job log not found") from error


@router.get("/{job_id}/logs", response_model=JobLogs, summary="The tail of a job's log")
async def get_job_logs(
    job_id: str,
    tail: int = Query(200, ge=1, le=1000),
    user: AuthenticatedUser = Depends(require_viewer),
):
    job = await asyncio.to_thread(jobs.get, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    authorize_job_read(job, user)
    log_path = str(job.get("log_path") or "")
    if not log_path:
        return {"job_id": job_id, "lines": []}
    lines = await asyncio.to_thread(_tail_log, Path(log_path), tail)
    return {"job_id": job_id, "lines": lines}
