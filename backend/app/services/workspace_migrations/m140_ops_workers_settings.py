"""Workspace schema migration 140: ops worker heartbeats and settings.

The ops diagnostics read two small stores rather than asking a process:

- ``ws_ops_workers``: one row per running worker, refreshed by its supervision
  loop, so an admin can see which pool is alive and what it is running without
  reaching into another process's memory (OPS-03);
- ``ws_ops_settings``: operator settings that must apply in every process, of
  which the first is the time-boxed debug-logging switch (``debug_logging``).

Rows carry timings, ids and counts only; no design content.
"""

from __future__ import annotations

from typing import Any


def migrate(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ws_ops_workers (
            worker_id TEXT PRIMARY KEY,
            pool TEXT,
            host TEXT,
            pid INTEGER,
            started_at TIMESTAMPTZ,
            concurrency INTEGER,
            memory_budget_mib INTEGER,
            running_jobs JSONB NOT NULL DEFAULT '[]'::jsonb,
            last_seen_at TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ws_ops_workers_last_seen
            ON ws_ops_workers(last_seen_at);
        CREATE TABLE IF NOT EXISTS ws_ops_settings (
            key TEXT PRIMARY KEY,
            value JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by TEXT NOT NULL DEFAULT ''
        );
        """
    )
