"""Ops job diagnostics under ``/api/ops`` (ADR scripting-and-mcp §11, OPS-04a).

Two admin reads: one job's timeline and one design bundle build's profile. Both
need ``PRISM_MCP_OPS_ENABLED`` (otherwise 404) and the ``ops_read`` policy
action, and both answer with identifiers, timings, counts and sizes only. The
services do the work on a worker thread; these handlers translate and type the
result. The MCP tools in ``app.services.mcp.tools_ops_jobs`` call these same
routes.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api._helpers import get_project_for_role_or_404
from app.core.errors import NotFound, caller_errors
from app.core.security import AuthenticatedUser, require_ops_reader
from app.services.ops.bundle_profile import bundle_build_profile
from app.services.ops.job_timeline import job_timeline

router = APIRouter(prefix="/api/ops", tags=["ops"])


class JobSummaryOut(BaseModel):
    job_id: str
    kind: str
    status: str
    stage: str
    project_id: Optional[str] = None
    repository_id: Optional[str] = None
    worker_pool: str
    priority: Optional[int] = None
    attempt: Optional[int] = None
    max_attempts: Optional[int] = None
    fence: Optional[int] = None
    lease_owner: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    cancel_requested_at: Optional[str] = None
    error_code: str
    #: Redacted by ``app.core.redaction`` before it reaches this model.
    error_message: str
    #: Parsed from the worker's message when it exited unexpectedly.
    exit_code: Optional[int] = None


class JobEventOut(BaseModel):
    id: int
    event_type: str
    stage: str
    percent: Optional[float] = None
    #: Redacted event details; the only free-form object in an ops result.
    details: dict[str, Any]
    created_at: Optional[str] = None


class QueueWaitOut(BaseModel):
    created_at: Optional[str] = None
    claimed_at: Optional[str] = None
    seconds: Optional[float] = None


class ClaimAttemptOut(BaseModel):
    worker_id: Optional[str] = None
    fence: Optional[int] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    seconds: Optional[float] = None
    outcome: str


class StageRunOut(BaseModel):
    stage: str
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    seconds: Optional[float] = None


class JobPhasesOut(BaseModel):
    queue_wait: Optional[QueueWaitOut] = None
    claims: list[ClaimAttemptOut]
    stages: list[StageRunOut]


class ResourceSlotOut(BaseModel):
    resource_name: str
    slot_number: int
    fence: Optional[int] = None
    lease_owner: str
    lease_expires_at: Optional[str] = None


class JobLockOut(BaseModel):
    lock_key: str
    mode: str
    fence: Optional[int] = None
    lease_owner: str
    lease_expires_at: Optional[str] = None


class JobTimelineOut(BaseModel):
    job: JobSummaryOut
    events: list[JobEventOut]
    #: More than 500 events exist; the list holds the oldest 500.
    events_truncated: bool
    phases: JobPhasesOut
    resource_slots: list[ResourceSlotOut]
    locks: list[JobLockOut]
    #: At most 200 lines, redacted.
    log_tail: list[str]
    #: A payload and a result can hold design data, so only their keys are shown.
    payload_keys: list[str]
    result_metadata_keys: list[str]


@router.get(
    "/jobs/{job_id}/timeline",
    response_model=JobTimelineOut,
    summary="A job's queue wait, claim attempts, stage durations and redacted log tail",
)
async def get_job_timeline(
    job_id: str,
    log_lines: int = Query(default=50, ge=1, le=200),
    user: AuthenticatedUser = Depends(require_ops_reader),
) -> JobTimelineOut:
    timeline = await asyncio.to_thread(job_timeline, job_id, log_lines=log_lines)
    if timeline is None:
        raise NotFound("Job not found", code="job-not-found")
    return JobTimelineOut.model_validate(timeline)


class BundleInvocationOut(BaseModel):
    #: Never the full argv: the CLI's basename and its subcommand.
    executable: str
    subcommand: str
    #: The bundle stage the run built or verified (parse, board, models...).
    stage: Optional[str] = None
    exit: Optional[int] = None
    elapsed_ms: Optional[float] = None
    peak_rss_bytes: Optional[int] = None


class BundleKicadStepOut(BaseModel):
    step: str
    runs: int
    exit: Optional[int] = None
    elapsed_ms: Optional[float] = None
    peak_rss_bytes: Optional[int] = None


class BundleCacheOut(BaseModel):
    upgrade_cache_hits: int
    upgrade_cache_misses: int
    refill_cache_hit: Optional[bool] = None
    models_cache_hit: Optional[bool] = None


class BundleBuildProfileOut(BaseModel):
    project_id: str
    commit: Optional[str] = None
    bundle_key: str
    revision_key: str
    design_key: Optional[str] = None
    state: str
    job_id: Optional[str] = None
    job_status: Optional[str] = None
    archive_bytes: int
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    wall_seconds: Optional[float] = None
    queue_wait_seconds: Optional[float] = None
    invocations: list[BundleInvocationOut]
    kicad_steps: list[BundleKicadStepOut]
    cache: BundleCacheOut
    stages: dict[str, str]
    failed_stages: list[str]
    partial_stages: list[str]
    #: The KiCad step or CLI invocation that took longest.
    slowest_step: Optional[str] = None
    #: The bundle's failure text, redacted.
    error: str


@router.get(
    "/projects/{project_id}/bundle-profile",
    response_model=BundleBuildProfileOut,
    summary="A design bundle build's per-invocation and per-KiCad-step profile",
)
async def get_bundle_build_profile(
    project_id: str,
    commit: Optional[str] = Query(default=None),
    job_id: Optional[str] = Query(default=None),
    user: AuthenticatedUser = Depends(require_ops_reader),
) -> BundleBuildProfileOut:
    project = await asyncio.to_thread(get_project_for_role_or_404, project_id, user.role)
    with caller_errors((ValueError, 400)):
        profile = await asyncio.to_thread(
            bundle_build_profile, project, commit=commit, job_id=job_id
        )
    if profile is None:
        raise NotFound("Design bundle not found", code="bundle-not-found")
    return BundleBuildProfileOut.model_validate(profile)


__all__ = ["router"]
