"""Workspace schema migration 134: MCP client grants and authorization codes.

An MCP client (Claude Desktop, Cursor) signs a person in through the browser
once; the resulting grant is what Settings lists and what a revoke ends. The
authorization code is stored only as a hash, single-use, with the request it was
bound to. The refresh token is stored hashed on the grant row; rotating it
replaces the hash, so the previous secret stops working.
"""

from __future__ import annotations

from typing import Any


def migrate(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ws_mcp_grants (
            id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            client_name TEXT NOT NULL,
            user_email TEXT NOT NULL,
            scopes JSONB NOT NULL,
            refresh_hash TEXT UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_used_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ws_mcp_codes (
            code_hash TEXT PRIMARY KEY,
            grant_payload JSONB NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ws_mcp_grants_user"
        " ON ws_mcp_grants(user_email)"
    )
