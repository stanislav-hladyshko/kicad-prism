"""The four ops actions under ``/api/ops`` (ADR scripting-and-mcp §11, OPS-06).

Each route needs ``PRISM_MCP_OPS_ENABLED`` (otherwise 404) and ``ops_act``,
except the debug-window read which needs ``ops_read``. They call the same
services the browser does and return what changed: a job's status, the rebuild
it queued, or the debug window in force. The MCP tools in
``app.services.mcp.tools_ops_actions`` call these routes.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api._helpers import get_project_for_role_or_404
from app.core.security import AuthenticatedUser, require_ops_actor, require_ops_reader
from app.services.ops import actions

router = APIRouter(prefix="/api/ops", tags=["ops"])


class JobCancelOut(BaseModel):
    """The job after a cancel request, and the status that request reached."""

    job_id: str
    status: str


class JobRequeueOut(BaseModel):
    """The job after a requeue: its new state, fence and attempt budget."""

    job_id: str
    status: str
    previous_status: str
    fence: int
    previous_fence: int
    attempt: int
    max_attempts: int


class RebuildBundleRequest(BaseModel):
    """Which commit to rebuild; omitted means the project's ``HEAD``."""

    commit: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{40}$")


class BundleRebuildOut(BaseModel):
    """The design-bundle rebuild an ops action queued."""

    job_id: Optional[str]
    commit: Optional[str]
    scope: str
    removed_bundles: int
    removed_aliases: int


class DebugLoggingRequest(BaseModel):
    """A debug window of one to thirty minutes for the named loggers."""

    minutes: int = Field(ge=1, le=actions.MAX_DEBUG_MINUTES)
    #: Prism's own loggers only: ``app`` or a child of it. Third-party loggers
    #: (HTTP clients, the database driver) can log URLs and query arguments.
    loggers: Optional[list[Annotated[str, Field(pattern=actions.LOGGER_PATTERN)]]] = Field(
        default=None, max_length=20
    )


class DebugLoggingWindow(BaseModel):
    """The DEBUG window currently in force."""

    #: The deadline, as an ISO 8601 timestamp, past which every process reverts.
    until: str
    loggers: list[str]


class DebugLoggingCleared(BaseModel):
    """Confirmation that the debug window was removed."""

    cleared: bool


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=JobCancelOut,
    summary="Ask an ops-selected job to cancel",
)
async def ops_cancel_job(
    job_id: str,
    user: AuthenticatedUser = Depends(require_ops_actor),
) -> JobCancelOut:
    result = await asyncio.to_thread(actions.cancel_job, job_id, requested_by=user.email)
    return JobCancelOut.model_validate(result)


@router.post(
    "/jobs/{job_id}/requeue",
    response_model=JobRequeueOut,
    summary="Requeue a failed or lease-expired job",
)
async def ops_requeue_job(
    job_id: str,
    user: AuthenticatedUser = Depends(require_ops_actor),
) -> JobRequeueOut:
    result = await asyncio.to_thread(actions.requeue_job, job_id, requested_by=user.email)
    return JobRequeueOut.model_validate(result)


@router.post(
    "/projects/{project_id}/rebuild-bundle",
    response_model=BundleRebuildOut,
    summary="Drop one commit's design bundle and queue a rebuild",
)
async def ops_rebuild_bundle(
    project_id: str,
    request: RebuildBundleRequest,
    user: AuthenticatedUser = Depends(require_ops_actor),
) -> BundleRebuildOut:
    await asyncio.to_thread(get_project_for_role_or_404, project_id, user.role)
    result = await asyncio.to_thread(
        actions.rebuild_bundle,
        project_id,
        commit=request.commit,
        requested_by=user.email,
    )
    return BundleRebuildOut.model_validate(result)


@router.post(
    "/debug-logging",
    response_model=DebugLoggingWindow,
    summary="Raise Prism's loggers to DEBUG for at most thirty minutes",
)
async def ops_set_debug_logging(
    request: DebugLoggingRequest,
    user: AuthenticatedUser = Depends(require_ops_actor),
) -> DebugLoggingWindow:
    result = await asyncio.to_thread(
        actions.set_debug_logging,
        request.minutes,
        loggers=request.loggers,
        by=user.email,
    )
    return DebugLoggingWindow.model_validate(result)


@router.delete(
    "/debug-logging",
    response_model=DebugLoggingCleared,
    summary="Clear the debug-logging window",
)
async def ops_clear_debug_logging(
    user: AuthenticatedUser = Depends(require_ops_actor),
) -> DebugLoggingCleared:
    await asyncio.to_thread(actions.clear_debug_logging, by=user.email)
    return DebugLoggingCleared(cleared=True)


@router.get(
    "/debug-logging",
    response_model=Optional[DebugLoggingWindow],
    summary="The debug-logging window in force, or null",
)
async def ops_get_debug_logging(
    user: AuthenticatedUser = Depends(require_ops_reader),
) -> Optional[DebugLoggingWindow]:
    result = await asyncio.to_thread(actions.current_debug_logging)
    return DebugLoggingWindow.model_validate(result) if result is not None else None


__all__ = ["router"]
