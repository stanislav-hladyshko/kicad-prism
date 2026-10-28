"""Workspace schema migration 131: idempotency_keys.

Frozen. Applied deployments recorded this body in ``ws_schema_migrations``;
edit nothing here, add a new migration instead.
"""

from __future__ import annotations

from typing import Any


def migrate(conn: Any) -> None:
    """Stored first responses for retried writes (ADR scripting-and-mcp §3.4).

    One row per caller, operation and key. ``state`` is ``pending`` while the
    winning request runs and ``done`` once its response is stored.
    """

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ws_idempotency_keys (
            caller TEXT NOT NULL, operation TEXT NOT NULL, key TEXT NOT NULL,
            request_hash TEXT NOT NULL, state TEXT NOT NULL,
            status_code INTEGER, headers JSONB, body BYTEA,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (caller, operation, key)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ws_idempotency_keys_created"
        " ON ws_idempotency_keys(created_at)"
    )
