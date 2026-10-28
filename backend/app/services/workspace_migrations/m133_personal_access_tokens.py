"""Workspace schema migration 133: personal access tokens.

Frozen. Applied deployments recorded this body in ``ws_schema_migrations``;
edit nothing here, add a new migration instead.
"""

from __future__ import annotations

from typing import Any


def migrate(conn: Any) -> None:
    """Scoped, expiring tokens a person uses from scripts and CI (ADR §3.6).

    Only the token's SHA-256 hex digest is stored; ``prefix`` keeps the first
    twelve characters so the owner can recognise a token without revealing it.
    """

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ws_personal_access_tokens (
            id TEXT PRIMARY KEY,
            user_email TEXT NOT NULL,
            name TEXT NOT NULL,
            scopes JSONB NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            prefix TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            last_used_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ws_pat_user"
        " ON ws_personal_access_tokens(user_email)"
    )
