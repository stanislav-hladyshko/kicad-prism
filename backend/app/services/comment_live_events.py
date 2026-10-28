"""Durable, per-project ordering for comment change notifications.

Call ``record_change`` inside the same transaction as the comment mutation.
PostgreSQL NOTIFY only wakes listeners; ``comment_change_events`` is the replay
source after a disconnected client or API worker resumes. A writer that opens
``queued_events()`` also gets one ``comment.changed`` in the event log for each
change, appended after its transaction commits.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from app.services import event_log


CHANNEL = "prism_comment_changes"

# The changes of the writer currently inside ``queued_events()``. The queue
# belongs to that writer, not to the pooled connection, so a change recorded
# on a path without a writer scope can never surface in a later checkout.
_QUEUE: ContextVar[list[dict[str, str]] | None] = ContextVar(
    "comment_changed_queue", default=None
)


def apply_schema(conn: Any) -> None:
    """Create the stream tables in the caller-selected comments schema."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS comment_stream_heads (
            project_id TEXT PRIMARY KEY,
            cursor BIGINT NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS comment_change_events (
            project_id TEXT NOT NULL,
            cursor BIGINT NOT NULL,
            comment_id TEXT NOT NULL,
            scope TEXT NOT NULL,
            base_commit TEXT,
            compare_commit TEXT,
            change_kind TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (project_id, cursor)
        );
        CREATE INDEX IF NOT EXISTS idx_comment_change_events_thread
            ON comment_change_events(project_id, comment_id, cursor);
        """,
        prepare=False,
    )


def current_cursor(conn: Any, project_id: str) -> int:
    row = conn.execute(
        "SELECT cursor FROM comment_stream_heads WHERE project_id = %s",
        (project_id,),
    ).fetchone()
    return int(row["cursor"]) if row else 0


def record_change(
    conn: Any,
    *,
    project_id: str,
    comment_id: str,
    scope: str,
    change_kind: str,
    base_commit: str | None = None,
    compare_commit: str | None = None,
) -> int:
    """Append a committed-change hint, ordered with all writers to this project.

    Incrementing one project row serializes concurrent writers: a later writer
    cannot obtain its cursor until the earlier writer commits or rolls back.
    The caller owns the transaction and must not acknowledge its mutation first.
    """
    if not project_id or not comment_id:
        raise ValueError("project_id and comment_id are required")
    if scope not in {"canvas", "comparison"}:
        raise ValueError("unsupported comment scope")
    if scope == "comparison" and not (base_commit and compare_commit):
        raise ValueError("comparison events require both commits")
    if scope == "canvas" and (base_commit or compare_commit):
        raise ValueError("canvas events cannot carry comparison commits")
    if change_kind not in {"upsert", "delete", "projection", "anchor"}:
        raise ValueError("unsupported comment change kind")

    conn.execute(
        "INSERT INTO comment_stream_heads(project_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (project_id,),
    )
    row = conn.execute(
        """
        UPDATE comment_stream_heads
        SET cursor = cursor + 1
        WHERE project_id = %s
        RETURNING cursor
        """,
        (project_id,),
    ).fetchone()
    cursor = int(row["cursor"])
    conn.execute(
        """
        INSERT INTO comment_change_events
            (project_id, cursor, comment_id, scope, base_commit, compare_commit, change_kind)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (project_id, cursor, comment_id, scope, base_commit, compare_commit, change_kind),
    )
    conn.execute(
        "SELECT pg_notify(%s, %s)",
        (CHANNEL, json.dumps({"projectId": project_id, "cursor": cursor}, separators=(",", ":"))),
    )
    _queue_event(project_id=project_id, comment_id=comment_id, change_kind=change_kind)
    return cursor


def _queue_event(*, project_id: str, comment_id: str, change_kind: str) -> None:
    """Remember one change for the enclosing writer to log after its commit.

    The event log is written outside the comment transaction: its head row
    serializes writers, and an event for a mutation that rolled back would be a
    lie. Outside ``queued_events()`` the change is not logged.
    """

    queued = _QUEUE.get()
    if queued is not None:
        queued.append(
            {"projectId": project_id, "commentId": comment_id, "changeKind": change_kind}
        )


@contextmanager
def queued_events() -> Iterator[list[dict[str, str]]]:
    """Collect the changes recorded inside the block; the caller logs them."""

    queued: list[dict[str, str]] = []
    token = _QUEUE.set(queued)
    try:
        yield queued
    finally:
        _QUEUE.reset(token)


def record_queued_events(events: list[dict[str, str]]) -> None:
    """Append one ``comment.changed`` event for each committed change."""

    for change in events:
        event_log.record_quietly(
            "comment.changed",
            project_id=change["projectId"],
            payload={"commentId": change["commentId"], "changeKind": change["changeKind"]},
        )


def record_thread_change(conn: Any, *, project_id: str, comment_id: str, change_kind: str) -> int:
    """``record_change`` for a root, reading its scope and comparison pair."""
    root = conn.execute(
        "SELECT scope, base_commit, compare_commit FROM comments WHERE project_id = %s AND id = %s",
        (project_id, comment_id),
    ).fetchone()
    if root is None:
        raise ValueError(f"Comment root missing for {comment_id}")
    return record_change(
        conn, project_id=project_id, comment_id=comment_id, scope=root["scope"],
        base_commit=root["base_commit"], compare_commit=root["compare_commit"], change_kind=change_kind,
    )


def changes_after(conn: Any, project_id: str, after: int, *, limit: int = 200) -> list[dict[str, Any]]:
    if after < 0 or not 1 <= limit <= 500:
        raise ValueError("invalid comment change cursor or limit")
    rows = conn.execute(
        """
        SELECT cursor, comment_id, scope, base_commit, compare_commit, change_kind, created_at
        FROM comment_change_events
        WHERE project_id = %s AND cursor > %s
        ORDER BY cursor ASC
        LIMIT %s
        """,
        (project_id, after, limit),
    ).fetchall()
    return [
        {
            "cursor": int(row["cursor"]),
            "commentId": row["comment_id"],
            "scope": row["scope"],
            "baseCommit": row["base_commit"],
            "compareCommit": row["compare_commit"],
            "changeKind": row["change_kind"],
            "createdAt": row["created_at"].isoformat().replace("+00:00", "Z"),
        }
        for row in rows
    ]
