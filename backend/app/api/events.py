"""The polling event route, ``GET /api/events`` (ADR §3.10).

The live socket (MCP-08b) and the producers (MCP-08c) come later; this route
replays the append-only log from a cursor, so CI and agents can poll without a
socket. A session needs only the viewer role; a bearer token additionally needs
``api:read`` through the ``read`` policy rule.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core import policy
from app.core.security import AuthenticatedUser, require_viewer
from app.services import event_log

router = APIRouter(prefix="/api/events", tags=["events"])


class EventOut(BaseModel):
    id: int
    kind: str
    projectId: str | None
    topic: str
    createdAt: str
    payload: dict[str, Any]


class EventPage(BaseModel):
    events: list[EventOut]
    next: int


@router.get("", response_model=EventPage, summary="List events after a cursor")
async def list_events(
    after: int = Query(0, ge=0),
    topic: list[str] | None = Query(None),
    limit: int = Query(200, ge=1, le=500),
    user: AuthenticatedUser = Depends(require_viewer),
) -> dict[str, Any]:
    policy.authorize(user, "read")
    events, next_cursor = await asyncio.to_thread(
        event_log.list_after,
        after,
        role=user.role,
        is_admin=policy.role_allows(user.role, "admin"),
        topics=topic,
        limit=limit,
    )
    return {"events": events, "next": next_cursor}
