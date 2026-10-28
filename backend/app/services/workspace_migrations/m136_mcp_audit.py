"""Workspace schema migration 136: read audit for MCP clients.

One row per ``tools/call`` and ``resources/read``: who read what, which project
and revision the result named, and whether the call succeeded, failed or was
refused by governance. Rows are pruned by ``PRISM_MCP_AUDIT_RETENTION_DAYS``.
"""

from __future__ import annotations

from typing import Any


def migrate(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ws_mcp_audit (
            id BIGSERIAL PRIMARY KEY,
            at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            user_email TEXT NOT NULL,
            auth_type TEXT NOT NULL DEFAULT '',
            client_id TEXT NOT NULL DEFAULT '',
            token_id TEXT NOT NULL DEFAULT '',
            method TEXT NOT NULL CHECK (method IN ('tools/call', 'resources/read', 'tasks/get', 'tasks/cancel')),
            tool TEXT NOT NULL,
            project_id TEXT,
            revision TEXT,
            outcome TEXT NOT NULL CHECK (outcome IN ('ok', 'error', 'refused')),
            code TEXT,
            duration_ms INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ws_mcp_audit_at"
        " ON ws_mcp_audit(at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ws_mcp_audit_client"
        " ON ws_mcp_audit(client_id, at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ws_mcp_audit_project"
        " ON ws_mcp_audit(project_id, at DESC)"
    )
