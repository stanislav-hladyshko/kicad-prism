"""Settings routes for MCP clients a person has connected (ADR §3.6).

A grant is listed the way the consent page described it: the client's name and
host, the scopes it holds, when it was connected and when it was last used. A
revoke ends every access token minted under the grant.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from app.core import policy
from app.core.security import AuthenticatedUser, require_admin, require_viewer
from app.services import mcp_auth_service, mcp_clients

router = APIRouter(prefix="/api/auth/mcp-clients", tags=["auth"])


class McpClientView(BaseModel):
    """A connected client as its owner may see it; never a token or a hash."""

    id: str
    client_name: str
    host: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None = None


class McpClientAdminView(McpClientView):
    user_email: str


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "client_name": row["client_name"],
        "host": mcp_clients.host_of(str(row["client_id"])),
        "scopes": [str(scope) for scope in (row.get("scopes") or [])],
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
    }


@router.get("", response_model=list[McpClientView])
async def list_mcp_clients(
    user: AuthenticatedUser = Depends(require_viewer),
) -> list[dict[str, Any]]:
    """The caller's own connected MCP clients."""
    rows = await asyncio.to_thread(mcp_auth_service.list_for, user.email)
    return [_view(row) for row in rows]


@router.delete("/{grant_id}", status_code=204, response_model=None)
async def revoke_mcp_client(
    grant_id: str,
    user: AuthenticatedUser = Depends(require_viewer),
) -> Response:
    """Revoke a client the caller connected, or any when the caller is an admin."""
    await asyncio.to_thread(
        mcp_auth_service.revoke_grant,
        grant_id,
        by_email=user.email,
        is_admin=policy.role_allows(user.role, "admin"),
    )
    return Response(status_code=204)


@router.get("/all", response_model=list[McpClientAdminView])
async def list_all_mcp_clients(
    _user: AuthenticatedUser = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Every person's connected clients, for an administrator."""
    rows = await asyncio.to_thread(mcp_auth_service.list_all)
    return [{**_view(row), "user_email": row["user_email"]} for row in rows]
