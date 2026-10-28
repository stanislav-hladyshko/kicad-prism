"""Bounded sampling profiles (ADR scripting-and-mcp §11, D26).

``sample`` reads every thread's stack with ``sys._current_frames`` at a fixed
rate and counts collapsed stacks: ``module:function;module:function;...``,
root first, the input format of flame-graph tools. Only module and function
names are kept, never local values, file paths or arguments, so a profile
carries no design content and nothing to redact.

A profile is taken either in the API process answering the request or in a
prism worker's supervisor, which claims a requested row in ``ws_ops_profiles``
from its supervision loop. Either way:

- one profile runs at a time across the deployment (a Postgres advisory lock);
- at most ``MAX_PER_HOUR`` start per hour;
- each lasts at most ``MAX_SECONDS``.
"""

from __future__ import annotations

import collections
import logging
import socket
import sys
import threading
import time
import uuid
from typing import Any

from psycopg.types.json import Jsonb

from app.services.postgres_database import database

logger = logging.getLogger(__name__)

MAX_SECONDS = 10.0
DEFAULT_HZ = 100
MAX_STACKS = 500
MAX_DEPTH = 64
MAX_PER_HOUR = 6
LOCK_KEY = 0x5052_4953_4D50_5246  # "PRISMPRF"
TARGETS = ("api", "worker")


class ProfileRefused(Exception):
    """A profile can't start now (busy, rate-limited or bad arguments)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _frame_label(frame: Any) -> str:
    module = frame.f_globals.get("__name__") or "?"
    return f"{module}:{frame.f_code.co_name}"


def _stack(frame: Any) -> str:
    labels: list[str] = []
    while frame is not None and len(labels) < MAX_DEPTH:
        labels.append(_frame_label(frame))
        frame = frame.f_back
    labels.reverse()
    return ";".join(labels)


def sample(seconds: float, *, hz: int = DEFAULT_HZ) -> dict[str, Any]:
    """Sample this process's threads for ``seconds``; return the collapsed stacks."""

    seconds = max(0.1, min(float(seconds), MAX_SECONDS))
    interval = 1.0 / max(1, min(int(hz), 1000))
    me = threading.get_ident()
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    counts: collections.Counter[str] = collections.Counter()
    samples = 0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for ident, frame in sys._current_frames().items():
            if ident == me:
                continue
            thread = names.get(ident) or "thread"
            counts[f"{thread};{_stack(frame)}"] += 1
        samples += 1
        time.sleep(interval)
    top = counts.most_common(MAX_STACKS)
    return {
        "host": socket.gethostname(),
        "pid": _pid(),
        "seconds": round(seconds, 3),
        "hz": round(1.0 / interval),
        "samples": samples,
        "stacks": [{"stack": stack, "count": count} for stack, count in top],
        "truncated": len(counts) > MAX_STACKS,
    }


def _pid() -> int:
    import os

    return os.getpid()


def _check_rate(conn: Any) -> None:
    row = conn.execute(
        "SELECT count(*) AS n FROM ws_ops_profiles WHERE created_at > now() - interval '1 hour'"
    ).fetchone()
    if int(row["n"]) >= MAX_PER_HOUR:
        raise ProfileRefused("profile-rate-limited", f"At most {MAX_PER_HOUR} profiles per hour")


def _insert(conn: Any, *, target: str, worker_id: str | None, seconds: float, requested_by: str,
            status: str) -> str:
    profile_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO ws_ops_profiles (id, target, worker_id, seconds, status, requested_by, started_at)
        VALUES (%s, %s, %s, %s, %s, %s, CASE WHEN %s = 'running' THEN now() END)
        """,
        (profile_id, target, worker_id, seconds, status, requested_by, status),
    )
    return profile_id


def _finish(profile_id: str, result: dict[str, Any] | None, *, error: str | None = None) -> None:
    with database.connection() as conn:
        conn.execute("SET search_path TO workspace, public")
        conn.execute(
            """
            UPDATE ws_ops_profiles
            SET status = %s, result = %s, error = %s, finished_at = now()
            WHERE id = %s
            """,
            ("failed" if error else "completed", Jsonb(result) if result else None, error, profile_id),
        )
        conn.commit()


def profile_here(seconds: float, *, requested_by: str) -> dict[str, Any]:
    """Profile the calling process now, holding the deployment-wide lock."""

    seconds = _seconds(seconds)
    with database.connection() as conn:
        conn.execute("SET search_path TO workspace, public")
        locked = conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (LOCK_KEY,)).fetchone()
        if not locked["locked"]:
            raise ProfileRefused("profile-busy", "Another profile is running")
        try:
            _check_rate(conn)
            profile_id = _insert(conn, target="api", worker_id=None, seconds=seconds,
                                 requested_by=requested_by, status="running")
            conn.commit()
            try:
                result = sample(seconds)
            except Exception as exc:  # noqa: BLE001
                _finish(profile_id, None, error=type(exc).__name__)
                raise
            _finish(profile_id, result)
            return get(profile_id) or {}
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
            conn.commit()


def request_worker(seconds: float, *, worker_id: str | None, requested_by: str) -> str:
    """Ask a prism worker to profile its supervisor; returns the profile id."""

    seconds = _seconds(seconds)
    with database.connection() as conn:
        conn.execute("SET search_path TO workspace, public")
        _check_rate(conn)
        busy = conn.execute(
            "SELECT 1 FROM ws_ops_profiles WHERE status IN ('requested', 'running') "
            "AND created_at > now() - interval '1 minute' LIMIT 1"
        ).fetchone()
        if busy:
            raise ProfileRefused("profile-busy", "Another profile is running")
        profile_id = _insert(conn, target="worker", worker_id=worker_id, seconds=seconds,
                             requested_by=requested_by, status="requested")
        conn.commit()
    return profile_id


def claim_for_worker(worker_id: str) -> dict[str, Any] | None:
    """A requested worker profile this worker should take, marked running."""

    with database.connection() as conn:
        conn.execute("SET search_path TO workspace, public")
        row = conn.execute(
            """
            UPDATE ws_ops_profiles SET status = 'running', worker_id = %s, started_at = now()
            WHERE id = (
                SELECT id FROM ws_ops_profiles
                WHERE status = 'requested' AND target = 'worker'
                  AND (worker_id IS NULL OR worker_id = %s)
                  AND created_at > now() - interval '1 minute'
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, seconds
            """,
            (worker_id, worker_id),
        ).fetchone()
        conn.commit()
    return dict(row) if row else None


def run_claimed(profile: dict[str, Any]) -> None:
    """Take a claimed worker profile (call from a thread, not the supervision loop)."""

    with database.connection() as conn:
        conn.execute("SET search_path TO workspace, public")
        locked = conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (LOCK_KEY,)).fetchone()
        if not locked["locked"]:
            _finish(str(profile["id"]), None, error="profile-busy")
            return
        try:
            _finish(str(profile["id"]), sample(float(profile["seconds"])))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Worker profile failed", exc_info=True)
            _finish(str(profile["id"]), None, error=type(exc).__name__)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
            conn.commit()


def get(profile_id: str) -> dict[str, Any] | None:
    with database.connection() as conn:
        conn.execute("SET search_path TO workspace, public")
        row = conn.execute(
            """
            SELECT id, target, worker_id, seconds, status, error, result, requested_by,
                   created_at, started_at, finished_at
            FROM ws_ops_profiles WHERE id = %s
            """,
            (profile_id,),
        ).fetchone()
    return dict(row) if row else None


def wait_for(profile_id: str, timeout: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while True:
        row = get(profile_id)
        if row is None or row["status"] in ("completed", "failed") or time.monotonic() >= deadline:
            return row
        time.sleep(0.5)


def prune(days: int = 7) -> int:
    with database.connection() as conn:
        conn.execute("SET search_path TO workspace, public")
        cursor = conn.execute(
            "DELETE FROM ws_ops_profiles WHERE created_at < now() - make_interval(days => %s)", (days,)
        )
        conn.commit()
        return cursor.rowcount or 0


def _seconds(seconds: float) -> float:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        raise ProfileRefused("invalid-profile", "seconds must be a number") from None
    if not 0.1 <= value <= MAX_SECONDS:
        raise ProfileRefused("invalid-profile", f"seconds must be between 0.1 and {MAX_SECONDS:g}")
    return value


__all__ = [
    "MAX_PER_HOUR", "MAX_SECONDS", "ProfileRefused", "claim_for_worker", "get", "profile_here",
    "prune", "request_worker", "run_claimed", "sample", "wait_for",
]
