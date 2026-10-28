"""Ops instrumentation (ADR scripting-and-mcp §11, D23–D24).

Per-minute route statistics and named counters, the request log, and a
``request_id`` on the rows a request creates (jobs, events, audit), so
``trace_request`` can join them.
"""

from __future__ import annotations

from typing import Any


def migrate(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ws_ops_route_stats (
            minute TIMESTAMPTZ NOT NULL,
            method TEXT NOT NULL,
            route TEXT NOT NULL,
            count BIGINT NOT NULL,
            status_4xx BIGINT NOT NULL DEFAULT 0,
            status_5xx BIGINT NOT NULL DEFAULT 0,
            buckets BIGINT[] NOT NULL,
            PRIMARY KEY (minute, method, route)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ws_ops_counters (
            minute TIMESTAMPTZ NOT NULL,
            name TEXT NOT NULL,
            value BIGINT NOT NULL,
            PRIMARY KEY (minute, name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ws_ops_requests (
            id BIGSERIAL PRIMARY KEY,
            request_id TEXT NOT NULL,
            at TIMESTAMPTZ NOT NULL,
            method TEXT NOT NULL,
            route TEXT NOT NULL,
            status INTEGER NOT NULL,
            duration_ms INTEGER NOT NULL,
            db_ms INTEGER NOT NULL DEFAULT 0,
            auth_type TEXT NOT NULL DEFAULT '',
            user_email TEXT NOT NULL DEFAULT '',
            client_id TEXT NOT NULL DEFAULT '',
            details JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ws_ops_requests_request_id ON ws_ops_requests (request_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ws_ops_requests_at ON ws_ops_requests (at DESC)")
    for table in ("ws_jobs", "ws_events", "ws_mcp_audit"):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS request_id TEXT")
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_request_id ON {table} (request_id) WHERE request_id IS NOT NULL"
        )
