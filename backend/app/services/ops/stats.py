"""Per-minute route statistics, counters and the request log (ADR §11, D23–D24).

Each process aggregates in memory and a daemon thread writes it out:

- every 60 s, finished minutes go to ``ws_ops_route_stats`` (one row per
  minute, method and route template: a count, 4xx and 5xx counts and a
  log-scale duration histogram) and ``ws_ops_counters`` (named counts such as
  cache hits). Rows are upserted by adding, so several processes share a row;
- every 5 s, buffered request rows go to ``ws_ops_requests``;
- hourly, rows past retention are deleted.

Recording never touches the database and never raises; a failed flush logs a
warning and keeps the data for the next try, bounded so memory can't grow
without limit. ``flush(force=True)`` at exit writes the current minute too,
for short-lived processes such as job runners.
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Jsonb

from app.services.postgres_database import database

logger = logging.getLogger(__name__)

#: Upper bounds of the duration histogram, in ms; one more bucket holds the rest.
BUCKETS_MS: tuple[int, ...] = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000)
STATS_RETENTION_DAYS = 7
REQUESTS_RETENTION_DAYS = 3
MAX_BUFFERED_REQUESTS = 5000
MAX_PENDING_KEYS = 20000

_lock = threading.Lock()
_routes: dict[tuple[int, str, str], list[Any]] = {}
_counters: dict[tuple[int, str], int] = {}
_requests: list[dict[str, Any]] = []
_flusher: threading.Thread | None = None
_process_kind = "api"


def _minute(now: float | None = None) -> int:
    return int((now if now is not None else time.time()) // 60) * 60


def bucket_index(duration_ms: float) -> int:
    for index, bound in enumerate(BUCKETS_MS):
        if duration_ms <= bound:
            return index
    return len(BUCKETS_MS)


def record_request(method: str, route: str, status: int, duration_ms: float, *, now: float | None = None) -> None:
    key = (_minute(now), method.upper(), route)
    with _lock:
        entry = _routes.get(key)
        if entry is None:
            if len(_routes) >= MAX_PENDING_KEYS:
                return
            entry = [0, 0, 0, [0] * (len(BUCKETS_MS) + 1)]
            _routes[key] = entry
        entry[0] += 1
        if 400 <= status < 500:
            entry[1] += 1
        elif status >= 500:
            entry[2] += 1
        entry[3][bucket_index(duration_ms)] += 1


def incr(name: str, amount: int = 1, *, now: float | None = None) -> None:
    """Add ``amount`` to a named counter for this minute (cache hits and the like)."""

    key = (_minute(now), name)
    with _lock:
        if key not in _counters and len(_counters) >= MAX_PENDING_KEYS:
            return
        _counters[key] = _counters.get(key, 0) + amount


def log_request(row: dict[str, Any]) -> None:
    with _lock:
        if len(_requests) < MAX_BUFFERED_REQUESTS:
            _requests.append(row)


def _take(force: bool) -> tuple[dict, dict, list]:
    current = _minute()
    with _lock:
        routes = {key: value for key, value in _routes.items() if force or key[0] < current}
        counters = {key: value for key, value in _counters.items() if force or key[0] < current}
        for key in routes:
            del _routes[key]
        for key in counters:
            del _counters[key]
        requests = list(_requests)
        _requests.clear()
    return routes, counters, requests


def _restore(routes: dict, counters: dict, requests: list) -> None:
    with _lock:
        for key, (count, c4, c5, buckets) in routes.items():
            entry = _routes.setdefault(key, [0, 0, 0, [0] * (len(BUCKETS_MS) + 1)])
            entry[0] += count
            entry[1] += c4
            entry[2] += c5
            entry[3] = [a + b for a, b in zip(entry[3], buckets)]
        for key, value in counters.items():
            _counters[key] = _counters.get(key, 0) + value
        room = MAX_BUFFERED_REQUESTS - len(_requests)
        _requests[:0] = requests[: max(0, room)]


def _write(routes: dict, counters: dict, requests: list) -> None:
    with database.connection() as conn:
        conn.execute("SET search_path TO workspace, public")
        for (minute, method, route), (count, c4, c5, buckets) in routes.items():
            conn.execute(
                """
                INSERT INTO ws_ops_route_stats (minute, method, route, count, status_4xx, status_5xx, buckets)
                VALUES (to_timestamp(%s), %s, %s, %s, %s, %s, %s)
                ON CONFLICT (minute, method, route) DO UPDATE SET
                    count = ws_ops_route_stats.count + EXCLUDED.count,
                    status_4xx = ws_ops_route_stats.status_4xx + EXCLUDED.status_4xx,
                    status_5xx = ws_ops_route_stats.status_5xx + EXCLUDED.status_5xx,
                    buckets = (
                        SELECT array_agg(a + b ORDER BY ord)
                        FROM unnest(ws_ops_route_stats.buckets, EXCLUDED.buckets) WITH ORDINALITY AS u(a, b, ord)
                    )
                """,
                (minute, method, route[:300], count, c4, c5, buckets),
            )
        for (minute, name), value in counters.items():
            conn.execute(
                """
                INSERT INTO ws_ops_counters (minute, name, value) VALUES (to_timestamp(%s), %s, %s)
                ON CONFLICT (minute, name) DO UPDATE SET value = ws_ops_counters.value + EXCLUDED.value
                """,
                (minute, name[:200], value),
            )
        for row in requests:
            conn.execute(
                """
                INSERT INTO ws_ops_requests
                    (request_id, at, method, route, status, duration_ms, db_ms, auth_type, user_email, client_id, details)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    row["request_id"], row["at"], row["method"], row["route"][:300], row["status"],
                    row["duration_ms"], row["db_ms"], row.get("auth_type") or "", row.get("user_email") or "",
                    row.get("client_id") or "", Jsonb({"process": _process_kind, **(row.get("details") or {})}),
                ),
            )
        conn.commit()


def flush(*, force: bool = False) -> None:
    routes, counters, requests = _take(force)
    if not (routes or counters or requests):
        return
    try:
        _write(routes, counters, requests)
    except Exception:  # noqa: BLE001 - statistics never take a process down
        logger.warning("Could not flush ops statistics; keeping them for the next try", exc_info=True)
        _restore(routes, counters, requests)


def prune() -> int:
    with database.connection() as conn:
        conn.execute("SET search_path TO workspace, public")
        removed = 0
        for table, column, days in (
            ("ws_ops_route_stats", "minute", STATS_RETENTION_DAYS),
            ("ws_ops_counters", "minute", STATS_RETENTION_DAYS),
            ("ws_ops_requests", "at", REQUESTS_RETENTION_DAYS),
        ):
            cursor = conn.execute(
                f"DELETE FROM {table} WHERE {column} < now() - make_interval(days => %s)", (days,)
            )
            removed += cursor.rowcount or 0
        conn.commit()
    return removed


def _run() -> None:
    last_stats = time.monotonic()
    last_prune = 0.0
    while True:
        time.sleep(5)
        now = time.monotonic()
        try:
            if now - last_stats >= 60:
                flush()
                last_stats = now
            else:
                _flush_requests_only()
            if now - last_prune >= 3600:
                prune()
                last_prune = now
        except Exception:  # noqa: BLE001
            logger.warning("Ops statistics upkeep failed", exc_info=True)


def _flush_requests_only() -> None:
    with _lock:
        requests = list(_requests)
        _requests.clear()
    if not requests:
        return
    try:
        _write({}, {}, requests)
    except Exception:  # noqa: BLE001
        logger.warning("Could not flush the request log", exc_info=True)
        _restore({}, {}, requests)


def start(process_kind: str) -> None:
    """Start this process's flusher once, and flush at exit."""

    global _flusher, _process_kind
    with _lock:
        if _flusher is not None:
            return
        _process_kind = process_kind
        _flusher = threading.Thread(target=_run, name="ops-stats", daemon=True)
        _flusher.start()
    atexit.register(lambda: flush(force=True))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["BUCKETS_MS", "bucket_index", "flush", "incr", "log_request", "prune", "record_request", "start"]
