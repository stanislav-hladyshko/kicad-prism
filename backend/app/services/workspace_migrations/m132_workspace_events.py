"""Workspace schema migration 132: workspace_events.

Frozen. Applied deployments record this body in ``ws_schema_migrations``;
edit nothing here, add a new migration instead.
"""

from __future__ import annotations

from typing import Any


def migrate(conn: Any) -> None:
    """The append-only event log behind ``GET /api/v1/events`` (ADR §3.10).

    ``NOTIFY prism_events`` is only a wake-up hint for the live socket; the
    table is the record a reconnecting reader replays from. Ids come from the
    one-row ``ws_event_head``, locked by each writer until it commits, so ids
    become visible in order and a reader's cursor never skips a late commit
    (a sequence would hand out ids that commit out of order).
    """

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ws_events (
            id BIGINT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            kind TEXT NOT NULL,
            project_id TEXT,
            topic TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb
        );
        CREATE INDEX IF NOT EXISTS idx_ws_events_project ON ws_events(project_id, id);
        CREATE INDEX IF NOT EXISTS idx_ws_events_topic ON ws_events(topic, id);
        CREATE TABLE IF NOT EXISTS ws_event_head (
            singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
            cursor BIGINT NOT NULL DEFAULT 0
        );
        INSERT INTO ws_event_head (singleton, cursor) VALUES (TRUE, 0) ON CONFLICT DO NOTHING;
        """
    )
