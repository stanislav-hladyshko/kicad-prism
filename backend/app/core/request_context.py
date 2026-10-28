"""The current request's id and database time (ADR scripting-and-mcp §11, D24).

``RequestStatsMiddleware`` opens a context per HTTP request. The context is a
mutable object held in a ``ContextVar``: ``asyncio.to_thread`` copies the
variable into the worker thread, and because the copy points at the same
object, time a thread spends holding a database connection is added to the
request that started it. Code with no request around it (workers, jobs) sees
``None`` and records nothing.
"""

from __future__ import annotations

import re
import threading
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, field

HEADER = "x-request-id"
_VALID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass
class RequestContext:
    request_id: str
    db_seconds: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_db_time(self, seconds: float) -> None:
        with self._lock:
            self.db_seconds += seconds


_current: ContextVar[RequestContext | None] = ContextVar("prism_request", default=None)


def current() -> RequestContext | None:
    return _current.get()


def request_id() -> str | None:
    context = _current.get()
    return context.request_id if context else None


def accepted_id(inbound: str | None) -> str | None:
    """An inbound ``X-Request-Id`` worth keeping, or ``None``."""

    value = (inbound or "").strip()
    return value if _VALID.match(value) else None


def begin(inbound: str | None = None) -> tuple[RequestContext, Token]:
    context = RequestContext(accepted_id(inbound) or uuid.uuid4().hex)
    return context, _current.set(context)


def end(token: Token) -> None:
    _current.reset(token)


def add_db_time(seconds: float) -> None:
    context = _current.get()
    if context is not None:
        context.add_db_time(seconds)


__all__ = ["HEADER", "RequestContext", "accepted_id", "add_db_time", "begin", "current", "end", "request_id"]
