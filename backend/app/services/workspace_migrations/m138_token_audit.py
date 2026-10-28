"""Workspace schema migration 138: token activity audit.

The MCP audit records what a client read or ran. An HTTP write made with a
bearer token is recorded beside those rows as ``method = 'http'``, so an
administrator sees what every token did rather than only the MCP calls. This
widens the method check to admit that row; the table and its indexes are
unchanged.
"""

from __future__ import annotations

from typing import Any


def migrate(conn: Any) -> None:
    statements = (
        """
        ALTER TABLE ws_mcp_audit
        DROP CONSTRAINT IF EXISTS ws_mcp_audit_method_check
        """,
        """
        ALTER TABLE ws_mcp_audit
        ADD CONSTRAINT ws_mcp_audit_method_check
        CHECK (method IN ('http', 'tools/call', 'resources/read', 'tasks/get', 'tasks/cancel'))
        """,
    )
    for statement in statements:
        conn.execute(statement)
