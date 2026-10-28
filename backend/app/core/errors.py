"""Typed domain errors and the one place they become HTTP responses.

A service refuses a request by raising a ``DomainError`` subclass. The class
fixes the status and a default machine ``code``; the message is written for the
caller, so it may reach the client. Routers do not translate these: the handler
``install_error_handlers`` registers renders every one the same way::

    {"detail": "<message>", "code": "<code>", ...extra}

That is the only error body Prism sends. ``detail`` is a string and keeps FastAPI's key so every existing
reader (browser, KiCad panel, scripts) keeps working. ``code`` is always present:
a service passes a specific one (``revision_conflict``, ``publication_required``,
``interface_not_ready`` ...) or the class default applies (``not_found``,
``stale_version`` ...). Extra fields (``currentRevision``, ``requiredRole`` ...)
sit beside it. ``headers`` (an ``ETag`` on a stale write) are sent with the
response.

The same handler set renders the framework's own errors in that shape:
FastAPI's request validation (422) joins its array into one message
(``"<field>: <msg>, ..."``, code ``validation_error``, the request input never
echoed), and a plain ``HTTPException`` (a router's, or Starlette's unknown
route / method) gets the code of its status: the typed class's code where one
exists (404 ``not_found``), else the status phrase (401 ``unauthorized``).
A ``HTTPException`` whose ``detail`` is not a string (the readiness payload)
keeps it as is beside the code.

Guarded writes (a whole System, a comment or reply, a catalog component) use
one conflict shape: the client sends ``If-Match`` with the ETag it read
(``app.core.preconditions``), a missing one is 428 (``PreconditionRequired``)
and an old one is 412 (``StaleVersion``) carrying the current ETag. 409
(``Conflict``) is for a write that contradicts state no ETag describes.
Pass ``status_code=`` only to keep a status an existing client depends on.

Untyped exceptions are never rendered here, so their text never reaches a
client. ``value_errors_as`` is the one remaining bridge for services that still
signal a refused input with a bare ``ValueError`` written for the caller.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from http import HTTPStatus

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class DomainError(Exception):
    """A refusal the caller can act on. Subclasses fix the status."""

    status_code: int = 400
    code: str = "error"
    default_detail: str = "Request refused"

    def __init__(
        self,
        message: str = "",
        *,
        code: str | None = None,
        status_code: int | None = None,
        detail: Any = None,
        headers: Mapping[str, str] | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(message)
        if code:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.detail = detail if detail is not None else (message or self.default_detail)
        self.headers = dict(headers or {})
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"detail": self.detail, "code": self.code, **self.extra}


class BadRequest(DomainError):
    """Bad request."""

    status_code = 400
    code = "bad_request"
    default_detail = "Bad request"


class Forbidden(DomainError):
    """Forbidden."""

    status_code = 403
    code = "forbidden"
    default_detail = "Forbidden"


class NotFound(DomainError):
    """Not found."""

    status_code = 404
    code = "not_found"
    default_detail = "Not found"


class Conflict(DomainError):
    """The request contradicts the current state."""

    status_code = 409
    code = "conflict"
    default_detail = "Conflict"


class StaleVersion(DomainError):
    """The resource changed since the caller read it."""

    status_code = 412
    code = "stale_version"
    default_detail = "The resource changed; reload it"


class Invalid(DomainError):
    """Invalid request."""

    status_code = 422
    code = "invalid"
    default_detail = "Invalid request"


class PreconditionRequired(DomainError):
    """A guarded write came without ``If-Match``."""

    status_code = 428
    code = "precondition_required"
    default_detail = "If-Match is required"


class UpstreamFailure(DomainError):
    """An upstream service failed."""

    status_code = 502
    code = "upstream_failure"
    default_detail = "An upstream service failed"


class Unavailable(DomainError):
    """Service unavailable."""

    status_code = 503
    code = "unavailable"
    default_detail = "Service unavailable"


def error_response(error: DomainError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content=error.body(), headers=error.headers or None)


async def _domain_error_handler(_request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, DomainError)
    return error_response(error)


def validation_message(problems: list[dict[str, Any]]) -> str:
    """FastAPI's validation array as one message: ``"<field>: <msg>, ..."``."""

    parts = []
    for problem in problems:
        loc = problem.get("loc") or ()
        parts.append(f"{loc[-1] if loc else 'Field'}: {problem.get('msg', 'Invalid value')}")
    return ", ".join(parts) or "Invalid request"


async def _validation_error_handler(_request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RequestValidationError)
    return JSONResponse(
        status_code=422,
        content={"detail": validation_message(list(error.errors())), "code": "validation_error"},
    )


def status_code_name(status_code: int) -> str:
    """The machine code for a bare status: the typed class's code, else the HTTP phrase."""

    cls = _CLASS_FOR_STATUS.get(status_code)
    if cls is not None:
        return cls.code
    try:
        return HTTPStatus(status_code).phrase.lower().replace(" ", "_").replace("-", "_")
    except ValueError:
        return DomainError.code


async def _http_exception_handler(_request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, StarletteHTTPException)
    headers = getattr(error, "headers", None)
    if error.status_code in (204, 304):  # no body allowed
        return JSONResponse(status_code=error.status_code, content=None, headers=headers)
    return JSONResponse(
        status_code=error.status_code,
        content={"detail": error.detail, "code": status_code_name(error.status_code)},
        headers=headers,
    )


#: Tests set this so a route whose response breaks its declared model fails loudly.
STRICT_RESPONSE_MODELS_ENV = "PRISM_STRICT_RESPONSE_MODELS"


async def _response_validation_handler(request: Request, error: Exception) -> JSONResponse:
    """A route's response did not fit its declared ``response_model``.

    Typing a route (ADR scripting-and-mcp §3.2) describes what it already
    returned, so a mismatch is a bug in the model, not in the response. Serve
    the response as the route produced it and log the mismatch, rather than
    turning a working route into a 500. Tests set ``PRISM_STRICT_RESPONSE_MODELS``
    and the mismatch fails them instead.
    """

    assert isinstance(error, ResponseValidationError)
    if os.environ.get(STRICT_RESPONSE_MODELS_ENV) == "1":
        raise error
    route = request.scope.get("route")
    logger.error(
        "Response of %s %s does not match its response model; served unvalidated: %s",
        request.method,
        getattr(route, "path", request.url.path),
        error.errors()[:5],
    )
    return JSONResponse(jsonable_encoder(error.body), status_code=getattr(route, "status_code", None) or 200)


def install_error_handlers(app: FastAPI) -> None:
    """Register the error handlers. ``app.main`` calls this once."""

    app.add_exception_handler(DomainError, _domain_error_handler)
    app.add_exception_handler(ResponseValidationError, _response_validation_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)


_CLASS_FOR_STATUS: dict[int, type[DomainError]] = {
    400: BadRequest,
    403: Forbidden,
    404: NotFound,
    409: Conflict,
    412: StaleVersion,
    422: Invalid,
    428: PreconditionRequired,
    502: UpstreamFailure,
    503: Unavailable,
}

ExceptionTypes = type[BaseException] | tuple[type[BaseException], ...]


def caller_error(error: BaseException, status_code: int) -> DomainError:
    """The typed error for an exception whose message an older service wrote for the caller."""

    return _CLASS_FOR_STATUS.get(status_code, DomainError)(str(error), status_code=status_code)


@contextmanager
def caller_errors(
    *rules: tuple[ExceptionTypes, int],
    keep_typed: bool = False,
    unexpected: str | None = None,
    unexpected_status: int = 500,
) -> Iterator[None]:
    """Render the listed exception types from the wrapped service call with a fixed status.

    The bridge for services that predate typed errors and signal a refusal with
    a built-in exception (``ValueError``, ``RuntimeError``, ``LookupError`` ...)
    whose message is written for the caller. Rules are tried in order, like
    ``except`` clauses. A typed error never matches when ``keep_typed`` is set,
    so a catalog conflict (also a ``ValueError``) keeps its own status and body;
    without it the route's status wins, as it did when the route caught the
    built-in type itself. Anything unlisted propagates and its text never
    reaches the client. ``unexpected`` keeps a route's old catch-all: any other
    exception is logged and answered ``unexpected_status`` (500) with that
    fixed message instead of its text.
    """

    try:
        yield
    except Exception as error:
        status = None if keep_typed and isinstance(error, DomainError) else _matching_status(error, rules)
        if status is not None:
            raise caller_error(error, status) from error
        if unexpected is None or isinstance(error, (DomainError, HTTPException)):
            raise
        logger.exception("%s", unexpected)
        raise caller_error(Exception(unexpected), unexpected_status) from error


def _matching_status(error: BaseException, rules: tuple[tuple[ExceptionTypes, int], ...]) -> int | None:
    for types, status in rules:
        if isinstance(error, types):
            return status
    return None


def value_errors_as(status_code: int = 400, *, keep_typed: bool = False):
    """``caller_errors`` for the common case: a bare ``ValueError`` becomes ``status_code``."""

    return caller_errors((ValueError, status_code), keep_typed=keep_typed)


def from_value_error(error: ValueError, status_code: int = 400) -> DomainError:
    """``caller_error`` for a router that already holds the ``ValueError``."""

    return caller_error(error, status_code)


__all__ = [
    "BadRequest",
    "caller_error",
    "caller_errors",
    "Conflict",
    "DomainError",
    "Forbidden",
    "Invalid",
    "NotFound",
    "PreconditionRequired",
    "StaleVersion",
    "Unavailable",
    "UpstreamFailure",
    "error_response",
    "from_value_error",
    "install_error_handlers",
    "status_code_name",
    "validation_message",
    "value_errors_as",
]
