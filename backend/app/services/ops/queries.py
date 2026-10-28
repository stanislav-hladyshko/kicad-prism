"""Slow queries from ``pg_stat_statements`` when the deployment loaded it (ADR §11, D25).

The extension is optional. Compose starts PostgreSQL with
``shared_preload_libraries=pg_stat_statements`` and migration 141 creates the
extension when the database role may; a managed instance may do neither. In
that case the answer is ``available: false`` with a reason, never an error, so
an agent can still use the other diagnostics.

Query text is PostgreSQL's normalised form. It can carry literals from a failed
statement, so it passes through the same redactor as every other ops string
(D22) and is cut to 2 kB. Rows are restricted to the current database.
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg import sql

from app.core.redaction import redact_text
from app.services.postgres_database import database

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_QUERY_CHARS = 2000
ORDERS = ("total", "mean")

_ORDER_COLUMNS = {"total": "total_exec_time", "mean": "mean_exec_time"}


def _loaded_libraries(raw: Any) -> set[str]:
    if not raw:
        return set()
    return {part.strip().strip('"') for part in str(raw).split(",") if part.strip()}


def _reason(conn: Any) -> str | None:
    """Why the view cannot be read, or ``None`` when it can."""

    extension = conn.execute(
        """
        SELECT n.nspname AS schema
        FROM pg_extension e
        JOIN pg_namespace n ON n.oid = e.extnamespace
        WHERE e.extname = 'pg_stat_statements'
        """
    ).fetchone()
    if extension is None:
        return (
            "pg_stat_statements is not installed; run migration 141 or "
            "CREATE EXTENSION pg_stat_statements as a superuser"
        )
    libraries = conn.execute(
        "SELECT current_setting('shared_preload_libraries', true) AS libraries"
    ).fetchone()
    if "pg_stat_statements" not in _loaded_libraries(libraries["libraries"] if libraries else None):
        return (
            "pg_stat_statements is installed but not loaded; add it to "
            "shared_preload_libraries and restart PostgreSQL"
        )
    return None


def _unavailable(order: str, reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason, "order": order, "queries": []}


def slow_queries(order: str = "total", limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """The slowest statements by total or mean time, redacted, or why there are none."""

    order = order if order in ORDERS else "total"
    limit = min(max(int(limit), 1), MAX_LIMIT)

    with database.connection() as conn:
        conn.execute("SET search_path TO workspace, public")
        reason = _reason(conn)
        if reason is not None:
            return _unavailable(order, reason)

        schema_row = conn.execute(
            """
            SELECT n.nspname AS schema
            FROM pg_extension e
            JOIN pg_namespace n ON n.oid = e.extnamespace
            WHERE e.extname = 'pg_stat_statements'
            """
        ).fetchone()
        schema = schema_row["schema"] if schema_row else "public"
        view = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier("pg_stat_statements"))
        statement = sql.SQL(
            """
            SELECT query, calls, total_exec_time, mean_exec_time, max_exec_time,
                   rows, shared_blks_hit, shared_blks_read
            FROM {view}
            WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
            ORDER BY {column} DESC NULLS LAST
            LIMIT %s
            """
        ).format(view=view, column=sql.Identifier(_ORDER_COLUMNS[order]))

        try:
            rows = conn.execute(statement, (limit,)).fetchall()
        except Exception:  # noqa: BLE001 - the extension is optional, not a request failure
            # The failed statement aborted the transaction; clear it so the
            # connection's normal exit can commit and return to the pool clean.
            conn.rollback()
            logger.warning("Could not read pg_stat_statements", exc_info=True)
            return _unavailable(order, "pg_stat_statements is installed but could not be read")

    return {
        "available": True,
        "reason": None,
        "order": order,
        "queries": [
            {
                "query": redact_text(row["query"], limit=MAX_QUERY_CHARS),
                "calls": int(row["calls"] or 0),
                "totalMs": round(float(row["total_exec_time"] or 0.0), 3),
                "meanMs": round(float(row["mean_exec_time"] or 0.0), 3),
                "maxMs": round(float(row["max_exec_time"] or 0.0), 3),
                "rows": int(row["rows"] or 0),
                "sharedBlksHit": int(row["shared_blks_hit"] or 0),
                "sharedBlksRead": int(row["shared_blks_read"] or 0),
            }
            for row in rows
        ],
    }


__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "MAX_QUERY_CHARS", "ORDERS", "slow_queries"]
