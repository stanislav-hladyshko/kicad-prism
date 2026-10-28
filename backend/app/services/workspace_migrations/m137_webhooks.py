"""Workspace schema migration 137: outbound webhooks (ADR §3.10, v5.2).

An administrator registers an HTTPS endpoint that receives the event log's
events, signed with Standard Webhooks. ``cursor`` is the last event id settled by
the endpoint (delivered, or skipped by its kind/project filter), so a pass reads
only what is new. ``next_attempt_at`` and ``failing_since`` are the retry state a
pass writes; ``secret_envelope`` holds the signing secret encrypted by
``trackers/secrets.py`` under the webhook's own AAD.

``ws_webhook_deliveries`` records one row per attempt so Settings can show what
an endpoint received and why it failed. Rows are pruned after 30 days.
"""

from __future__ import annotations

from typing import Any


def migrate(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ws_webhooks (
            id UUID PRIMARY KEY,
            url TEXT NOT NULL,
            event_kinds TEXT[] NOT NULL,
            project_ids TEXT[],
            secret_envelope TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'failing', 'disabled')),
            cursor BIGINT NOT NULL,
            failing_since TIMESTAMPTZ,
            next_attempt_at TIMESTAMPTZ,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_status INTEGER,
            last_error TEXT,
            last_delivery_at TIMESTAMPTZ,
            created_by TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ws_webhook_deliveries (
            id BIGSERIAL PRIMARY KEY,
            webhook_id UUID NOT NULL REFERENCES ws_webhooks(id) ON DELETE CASCADE,
            event_id BIGINT,
            at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            status_code INTEGER,
            ok BOOLEAN NOT NULL,
            error TEXT,
            duration_ms INTEGER
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ws_webhook_deliveries_webhook"
        " ON ws_webhook_deliveries(webhook_id, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ws_webhooks_due"
        " ON ws_webhooks(status, next_attempt_at)"
    )
