"""Workspace schema migration 141: pg_stat_statements (ADR §11, D25, OPS-04b).

``get_slow_queries`` reads ``pg_stat_statements`` when the server has the
extension loaded. Creating it needs a role with the privilege to install an
extension and a server whose ``shared_preload_libraries`` allows it; neither is
guaranteed on a managed instance. The attempt therefore runs inside a
savepoint: a refusal rolls back only this statement, the migration records
itself, and the diagnostic answers ``available: false`` with the reason.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def migrate(conn: Any) -> None:
    conn.execute("SAVEPOINT pg_stat_statements")
    try:
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements WITH SCHEMA public")
    except Exception:  # noqa: BLE001 - optional extension; the migration must succeed either way
        conn.execute("ROLLBACK TO SAVEPOINT pg_stat_statements")
        logger.warning(
            "pg_stat_statements could not be created; get_slow_queries will report it unavailable",
            exc_info=True,
        )
    finally:
        conn.execute("RELEASE SAVEPOINT pg_stat_statements")
