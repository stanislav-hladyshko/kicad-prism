"""``capture_profile`` behind ``/api/ops/profiles`` (ADR scripting-and-mcp §11, D26).

``POST`` takes a bounded sampling profile, of the API process that answers or
of a prism worker's supervisor, and returns the collapsed stacks. A worker
profile is picked up within about a second; if it hasn't finished when the
wait ends, the answer has ``status`` ``requested`` or ``running`` and ``GET``
fetches it later.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.errors import BadRequest, Conflict, NotFound
from app.core.security import AuthenticatedUser, require_ops_reader
from app.services.ops import profiler

router = APIRouter(prefix="/api/ops", tags=["ops"])

WORKER_WAIT_EXTRA_SECONDS = 15.0


class ProfileRequest(BaseModel):
    target: Literal["api", "worker"] = "api"
    #: A specific prism worker (``pool:host:pid:id``); any live worker when omitted.
    workerId: Optional[str] = None
    seconds: float = Field(default=5.0, ge=0.1, le=profiler.MAX_SECONDS)


class ProfileStack(BaseModel):
    stack: str
    count: int


class OpsProfile(BaseModel):
    id: str
    target: str
    workerId: Optional[str] = None
    status: str
    error: Optional[str] = None
    seconds: float
    requestedBy: str
    createdAt: datetime
    startedAt: Optional[datetime] = None
    finishedAt: Optional[datetime] = None
    host: Optional[str] = None
    pid: Optional[int] = None
    samples: Optional[int] = None
    hz: Optional[int] = None
    #: Collapsed stacks, ``thread;module:function;...``, most frequent first.
    stacks: list[ProfileStack] = []
    truncated: bool = False


def _view(row: dict) -> OpsProfile:
    result = row.get("result") or {}
    return OpsProfile(
        id=row["id"],
        target=row["target"],
        workerId=row.get("worker_id"),
        status=row["status"],
        error=row.get("error"),
        seconds=float(row["seconds"]),
        requestedBy=row.get("requested_by") or "",
        createdAt=row["created_at"],
        startedAt=row.get("started_at"),
        finishedAt=row.get("finished_at"),
        host=result.get("host"),
        pid=result.get("pid"),
        samples=result.get("samples"),
        hz=result.get("hz"),
        stacks=[ProfileStack(**item) for item in result.get("stacks") or []],
        truncated=bool(result.get("truncated")),
    )


def _refused(error: profiler.ProfileRefused) -> Exception:
    if error.code == "invalid-profile":
        return BadRequest(error.detail, code=error.code)
    return Conflict(error.detail, code=error.code)


@router.post("/profiles", response_model=OpsProfile, summary="Take a bounded sampling profile")
async def capture_profile(
    request: ProfileRequest, user: AuthenticatedUser = Depends(require_ops_reader)
) -> OpsProfile:
    try:
        if request.target == "api":
            row = await asyncio.to_thread(profiler.profile_here, request.seconds, requested_by=user.email)
        else:
            profile_id = await asyncio.to_thread(
                profiler.request_worker, request.seconds, worker_id=request.workerId, requested_by=user.email
            )
            row = await asyncio.to_thread(
                profiler.wait_for, profile_id, request.seconds + WORKER_WAIT_EXTRA_SECONDS
            )
    except profiler.ProfileRefused as error:
        raise _refused(error) from None
    if not row:
        raise NotFound("Profile not found", code="profile-not-found")
    return _view(row)


@router.get("/profiles/{profile_id}", response_model=OpsProfile, summary="One sampling profile")
async def get_profile(profile_id: str, user: AuthenticatedUser = Depends(require_ops_reader)) -> OpsProfile:
    row = await asyncio.to_thread(profiler.get, profile_id)
    if not row:
        raise NotFound("Profile not found", code="profile-not-found")
    return _view(row)


__all__ = ["router"]
