"""Workspace schema migration 135: MCP governance.

The one row an administrator edits in Settings → MCP: the projects MCP clients
may read (every project the caller can see, or a list) and the tools that are
switched off. A single row keeps "the workspace's policy" one value rather than
a key/value table whose keys nothing registers.
"""

from __future__ import annotations

from typing import Any


def migrate(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ws_mcp_governance (
            singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
            project_scope TEXT NOT NULL DEFAULT 'all'
                CHECK (project_scope IN ('all', 'listed')),
            project_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            disabled_tools JSONB NOT NULL DEFAULT '[]'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    conn.execute(
        """
        INSERT INTO ws_mcp_governance (singleton)
        VALUES (TRUE)
        ON CONFLICT (singleton) DO NOTHING
        """
    )
