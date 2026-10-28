"""Settings and audit routes for MCP governance (ADR scripting-and-mcp §3.11, D7).

``GET/PUT /api/settings/mcp`` is the policy an administrator edits: the project
scope and the disabled tools, plus the registry so Settings can render one
switch per tool. ``GET /api/admin/mcp-audit`` reads what MCP clients and bearer
tokens actually did, newest first, with a cursor so the table can ask for more.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.security import AuthenticatedUser, require_admin
from app.services.mcp import audit, governance

router = APIRouter(prefix="/api/settings/mcp", tags=["settings"])
admin_router = APIRouter(prefix="/api/admin/mcp-audit", tags=["admin"])


class McpToolView(BaseModel):
    name: str
    title: str


class McpGovernanceView(governance.McpGovernance):
    """The saved policy plus every registered tool, disabled or not."""

    tools: list[McpToolView]


class McpAuditRow(BaseModel):
    id: int
    at: datetime
    user_email: str
    auth_type: str
    client_id: str
    token_id: str
    method: Literal["http", "tools/call", "resources/read", "tasks/get", "tasks/cancel"]
    tool: str
    project_id: str | None = None
    revision: str | None = None
    outcome: Literal["ok", "error", "refused"]
    code: str | None = None
    duration_ms: int


class McpAuditPage(BaseModel):
    rows: list[McpAuditRow]
    next: int | None


@router.get("", response_model=McpGovernanceView)
async def get_mcp_governance(
    _user: AuthenticatedUser = Depends(require_admin),
) -> dict[str, Any]:
    value = await asyncio.to_thread(governance.current)
    return {**value.model_dump(by_alias=True), "tools": governance.tools_for_ui()}


@router.put("", response_model=governance.McpGovernance)
async def put_mcp_governance(
    body: governance.McpGovernance,
    user: AuthenticatedUser = Depends(require_admin),
) -> governance.McpGovernance:
    return await asyncio.to_thread(governance.save, body, role=user.role)


@admin_router.get("", response_model=McpAuditPage)
async def list_mcp_audit(
    client_id: str | None = Query(default=None),
    user: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    tool: str | None = Query(default=None),
    token_id: str | None = Query(default=None),
    kind: Literal["read", "write"] | None = Query(default=None),
    before: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
    _user: AuthenticatedUser = Depends(require_admin),
) -> dict[str, Any]:
    rows, next_cursor = await asyncio.to_thread(
        audit.list_page,
        client_id=client_id,
        user_email=user,
        project_id=project_id,
        tool=tool,
        token_id=token_id,
        kind=kind,
        before=before,
        limit=limit,
    )
    return {"rows": rows, "next": next_cursor}


__all__ = ["admin_router", "router"]
