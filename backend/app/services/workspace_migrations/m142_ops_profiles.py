"""Sampling profiles taken by ops (ADR scripting-and-mcp §11, D26)."""

from __future__ import annotations

from typing import Any


def migrate(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ws_ops_profiles (
            id TEXT PRIMARY KEY,
            target TEXT NOT NULL CHECK (target IN ('api', 'worker')),
            worker_id TEXT,
            seconds DOUBLE PRECISION NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('requested', 'running', 'completed', 'failed')),
            error TEXT,
            result JSONB,
            requested_by TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ws_ops_profiles_created ON ws_ops_profiles (created_at DESC)")
