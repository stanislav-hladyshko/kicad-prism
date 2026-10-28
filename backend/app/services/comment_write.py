"""Comment and reply row writes that stream their change.

The rule (``docs/architecture/live-comments.md``): every mutation of a root or
reply appends a change event in the transaction that makes it. Each function
here takes the caller's open connection, writes the row, records what the
rule needs (a ``comment_revisions`` row, which streams through
``comments_revisions.record_revision``, or an explicit event) and commits or
rolls back with the caller.

Who calls it: the store's thread writes (``comments_store_writes``), the
tracker's inbound replies and system notes, and the reply executor. Edits,
status changes and tombstones go through ``comments_revisions``, which
streams the same way. Nothing outside these two modules and the store writes
comment rows (``tests/test_comment_write.py`` enforces it).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Mapping, Optional

from app.services import comment_live_events, comments_revisions
from app.services.comments_revisions import Editor

_JSON_COLUMNS = frozenset({"mentions", "metadata"})


def new_comment_id() -> str:
    return f"c_{uuid.uuid4().hex[:8]}"


def new_reply_id() -> str:
    return f"r_{uuid.uuid4().hex[:8]}"


def insert_root(
    conn: Any,
    *,
    columns: Mapping[str, Any],
    editor: Editor,
    content_format: str,
) -> None:
    """Insert a root at revision 1 and record its creation (streams an upsert).

    ``columns`` are the ``comments`` columns as the caller normalized them;
    ``id``, ``project_id``, ``content``, ``severity``, ``comment_class`` and
    ``mentions`` (a list) are required.
    """
    names = list(columns)
    values = [json.dumps(columns[name]) if name in _JSON_COLUMNS else columns[name] for name in names]
    placeholders = ", ".join("%s::jsonb" if name in _JSON_COLUMNS else "%s" for name in names)
    conn.execute(
        f"INSERT INTO comments({', '.join(names)}, revision) VALUES({placeholders}, 1)",
        tuple(values),
    )
    comments_revisions.record_revision(
        conn, project_id=columns["project_id"], target_kind=comments_revisions.ROOT, target_id=columns["id"],
        revision=1, change_kind=comments_revisions.CHANGE_CREATE, editor=editor,
        content=columns["content"], severity=columns["severity"], comment_class=columns["comment_class"],
        mentions=columns["mentions"], content_format=content_format,
    )


def insert_reply(
    conn: Any,
    *,
    project_id: str,
    comment_id: str,
    content: str,
    editor: Editor,
    content_format: Optional[str] = None,
    reply_id: Optional[str] = None,
    require_live_root: bool = False,
) -> Optional[str]:
    """Insert a reply at revision 1 and record its creation (streams an upsert).

    The author columns come from ``editor``. Every reply, Prism's or the
    tracker's, is stamped with the transaction's ``NOW()`` (one clock);
    ``content_format`` defaults to the column default (``plain``),
    and the revision then records none, as tracker-written replies always
    have. With ``require_live_root`` nothing is written under a tombstoned
    root and ``None`` comes back.
    """
    reply_id = reply_id or new_reply_id()
    root_guard = " AND deleted_at IS NULL" if require_live_root else ""
    conn.execute(
        f"""
        INSERT INTO comment_replies(
            id, comment_id, project_id, author, timestamp, content,
            author_user_id, author_kind, author_client_id, revision, updated_at, origin, content_format
        )
        SELECT %s, %s, %s, %s, NOW(), %s, %s, %s, %s, 1, NOW(), %s, COALESCE(%s, 'plain')
        FROM comments
        WHERE project_id = %s AND id = %s{root_guard}
        """,
        (
            reply_id, comment_id, project_id, editor.display, content,
            editor.user_id, editor.kind, editor.client_id, editor.origin, content_format,
            project_id, comment_id,
        ),
    )
    inserted = conn.execute(
        "SELECT 1 FROM comment_replies WHERE project_id = %s AND id = %s",
        (project_id, reply_id),
    ).fetchone()
    if not inserted:
        if require_live_root:
            return None
        raise LookupError(f"comment {comment_id} missing for reply {reply_id}")
    comments_revisions.record_revision(
        conn, project_id=project_id, target_kind=comments_revisions.REPLY, target_id=reply_id,
        revision=1, change_kind=comments_revisions.CHANGE_CREATE, editor=editor,
        content=content, content_format=content_format,
    )
    return reply_id


def set_reply_sync_state(
    conn: Any,
    *,
    reply_id: str,
    state: Optional[str],
    project_id: Optional[str] = None,
    streamed_by_caller: bool = False,
) -> None:
    """Set or clear a reply's ``sync_state`` (``unsynced_local`` or ``None``).

    ``comment_replies`` has no trigger, so this streams a ``projection``
    event unless ``streamed_by_caller`` says the same transaction already
    streams the thread: the reply's own create revision, or a ``sync_ops`` /
    ``tracked_replies`` change whose projection trigger appends the event.
    """
    if project_id is None:
        row = conn.execute(
            "UPDATE comment_replies SET sync_state = %s WHERE id = %s RETURNING project_id, comment_id",
            (state, reply_id),
        ).fetchone()
    else:
        row = conn.execute(
            "UPDATE comment_replies SET sync_state = %s WHERE project_id = %s AND id = %s RETURNING project_id, comment_id",
            (state, project_id, reply_id),
        ).fetchone()
    if row is not None and not streamed_by_caller:
        comment_live_events.record_thread_change(
            conn, project_id=row["project_id"], comment_id=row["comment_id"], change_kind="projection",
        )
