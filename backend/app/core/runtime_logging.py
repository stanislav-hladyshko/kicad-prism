"""Time-boxed debug logging for the ops seam (ADR scripting-and-mcp §11, OPS-03).

Prism configures INFO logging once in each process, so turning DEBUG on for a
few minutes previously meant editing code or restarting. :func:`set_debug`
stores an operator's request in ``ws_ops_settings`` under ``debug_logging``;
every process runs :func:`apply` from a daemon poller started at its own start
(the API lifespan and the prism worker), which sets the named loggers to DEBUG
until the deadline (at most thirty minutes) and then puts them back.

The setting lives in the database, so one action reaches the API and every
worker. A level a logger already had explicitly is restored; one that had no
level of its own (``NOTSET``) goes back to inheriting, and the root logger goes
back to INFO. Applying is idempotent and never raises: a database outage must
not take down the process whose logging it describes.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator

from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)

MINUTES_MIN = 1
MINUTES_MAX = 30
DEFAULT_LOGGERS: tuple[str, ...] = ("app",)
SETTING_KEY = "debug_logging"
POLL_SECONDS = 15.0

#: The level a logger had before this process applied the debug window.
_APPLIED: dict[str, int] = {}
_APPLY_LOCK = threading.Lock()

_POLLER: threading.Thread | None = None
_POLLER_LOCK = threading.Lock()
_STOP = threading.Event()


@contextmanager
def connect() -> Iterator[Any]:
    """A workspace connection; imported lazily so this module stays import-light."""

    from app.services.postgres_database import database

    with database.connection() as conn:
        conn.execute("SET search_path TO workspace, public")
        yield conn


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _parse_until(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def _names(loggers: Iterable[str] | None) -> list[str]:
    seen: list[str] = []
    for name in loggers or DEFAULT_LOGGERS:
        text = str(name or "").strip()
        if text and text not in seen:
            seen.append(text)
    return seen or list(DEFAULT_LOGGERS)


def _read_setting() -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM ws_ops_settings WHERE key = %s", (SETTING_KEY,)
        ).fetchone()
    value = row["value"] if row is not None else None
    return dict(value) if isinstance(value, dict) else None


def set_debug(
    minutes: int,
    *,
    loggers: list[str] | None = None,
    by: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Store a debug-logging window and return what was stored.

    ``minutes`` is bounded to one through thirty: enough to watch a symptom,
    short enough that nobody forgets it. At least one logger name is required;
    the default is ``app``, the parent of Prism's own loggers.
    """

    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        raise ValueError("minutes must be an integer between 1 and 30") from None
    if not MINUTES_MIN <= minutes <= MINUTES_MAX:
        raise ValueError(
            f"minutes must be between {MINUTES_MIN} and {MINUTES_MAX}"
        )

    value = {
        "until": _iso(_as_utc(now or _now()) + timedelta(minutes=minutes)),
        "loggers": _names(loggers),
    }
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO ws_ops_settings (key, value, updated_at, updated_by)
            VALUES (%s, %s, NOW(), %s)
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = NOW(),
                updated_by = EXCLUDED.updated_by
            """,
            (SETTING_KEY, Jsonb(value), str(by or "")),
        )
    return value


def clear(by: str = "") -> None:
    """Remove the debug window; the next :func:`apply` restores INFO levels."""

    with connect() as conn:
        conn.execute("DELETE FROM ws_ops_settings WHERE key = %s", (SETTING_KEY,))
    logger.info("Debug logging cleared by %s", by or "unknown")


def current(*, now: datetime | None = None) -> dict[str, Any] | None:
    """The active debug window, or ``None`` when absent, expired or unreadable."""

    try:
        value = _read_setting()
    except Exception:
        logger.warning("Could not read the debug-logging setting", exc_info=True)
        return None
    if value is None:
        return None
    until = _parse_until(value.get("until"))
    if until is None or _as_utc(now or _now()) >= until:
        return None
    return value


def _restore(name: str) -> None:
    original = _APPLIED.pop(name, None)
    target = logging.getLogger(name)
    if not name or name == "root":
        target.setLevel(logging.INFO)
    elif original is None or original == logging.NOTSET:
        target.setLevel(logging.NOTSET)
    else:
        target.setLevel(original)


def _set_levels(names: Iterable[str]) -> None:
    """Set exactly ``names`` to DEBUG and restore every logger that left the set."""

    desired = list(dict.fromkeys(names))
    with _APPLY_LOCK:
        for name in tuple(_APPLIED):
            if name not in desired:
                _restore(name)
        for name in desired:
            if name in _APPLIED:
                continue
            target = logging.getLogger(name)
            _APPLIED[name] = target.level
            target.setLevel(logging.DEBUG)


def apply(*, now: datetime | None = None) -> None:
    """Apply the stored window, or restore levels once it is past or cleared.

    Called every ``POLL_SECONDS`` by :func:`start_poller`. A failed read leaves
    the levels as they are: an outage must not silently end a debug window that
    is still recorded, and it must never raise into the poller.
    """

    try:
        value = _read_setting()
    except Exception:
        logger.warning("Could not read the debug-logging setting", exc_info=True)
        return

    try:
        until = _parse_until(value.get("until")) if value else None
        active = bool(value) and until is not None and _as_utc(now or _now()) < until
        names = _names(value.get("loggers")) if active and value else []
        _set_levels(names)
    except Exception:
        logger.warning("Could not apply the debug-logging setting", exc_info=True)


def _poll() -> None:
    while True:
        apply()
        if _STOP.wait(POLL_SECONDS):
            return


def start_poller() -> None:
    """Start this process's one debug-logging poller; safe to call twice."""

    global _POLLER
    with _POLLER_LOCK:
        if _POLLER is not None and _POLLER.is_alive():
            return
        _STOP.clear()
        _POLLER = threading.Thread(
            target=_poll, name="runtime-logging", daemon=True
        )
        _POLLER.start()


def _stop_poller() -> None:
    """Stop the poller for a test that inspects levels directly."""

    global _POLLER
    with _POLLER_LOCK:
        thread, _POLLER = _POLLER, None
        _STOP.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=POLL_SECONDS + 5)


__all__ = [
    "DEFAULT_LOGGERS",
    "MINUTES_MAX",
    "MINUTES_MIN",
    "POLL_SECONDS",
    "SETTING_KEY",
    "apply",
    "clear",
    "connect",
    "current",
    "set_debug",
    "start_poller",
]
