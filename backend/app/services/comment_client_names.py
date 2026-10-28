"""Display names for the clients that wrote agent comments (ADR §10, D16).

A comment written through MCP stores the client's id, which is its Client ID
Metadata Document URL (``https://claude.ai/oauth/claude-code-client-metadata``).
People want the name that document declared ("Claude Code"). Prism kept that
name when the user approved the client (``ws_mcp_grants.client_name``), so a
listing looks it up here rather than fetching the document again.

Names are cached per process for a few minutes: a listing calls this once per
comment and reply, and a client's name almost never changes. A lookup that
fails answers ``None`` and the UI falls back to the id; a name is never worth
failing a comment listing for.
"""

from __future__ import annotations

import logging
import threading
import time

from app.services.postgres_database import database

logger = logging.getLogger(__name__)

CACHE_SECONDS = 300

_cache: dict[str, tuple[float, str | None]] = {}
_lock = threading.Lock()


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _lookup(client_id: str) -> str | None:
    with database.connection() as conn:
        row = conn.execute(
            """
            SELECT client_name
            FROM workspace.ws_mcp_grants
            WHERE client_id = %s AND client_name <> ''
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (client_id,),
        ).fetchone()
    if not row:
        return None
    name = row["client_name"] if isinstance(row, dict) else row[0]
    return str(name).strip() or None


def name_for(client_id: str | None) -> str | None:
    """The name ``client_id`` declared when it was approved, or ``None``."""

    if not client_id:
        return None
    now = time.monotonic()
    with _lock:
        cached = _cache.get(client_id)
        if cached and cached[0] > now:
            return cached[1]
    try:
        name = _lookup(client_id)
    except Exception:  # noqa: BLE001 - a name is never worth failing a listing
        logger.warning("Could not look up the name of client %s", client_id, exc_info=True)
        return None
    with _lock:
        _cache[client_id] = (now + CACHE_SECONDS, name)
    return name


__all__ = ["CACHE_SECONDS", "clear_cache", "name_for"]
