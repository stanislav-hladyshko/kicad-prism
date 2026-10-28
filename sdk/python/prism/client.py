"""The hand-written half of the KiCAD Prism Python SDK.

``GeneratedClient`` (``prism._generated``) carries one method per ``/api/v1``
operation. This module adds what OpenAPI cannot describe: the base URL and
token, bearer auth, idempotency keys on writes, error parsing, pagination and
job waiting.

The transport is injectable so tests (and unusual deployments) can replace
``urllib`` without patching the client:

    def transport(method, url, headers, body):
        return 200, {"content-type": "application/json"}, b"[]"

    client = Client("http://localhost:8000", token="...", transport=transport)
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Callable, Iterator, Mapping
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import quote, urlencode

from ._generated import GeneratedClient
from ._version import __version__

__all__ = ["Client", "PrismError", "__version__"]

Transport = Callable[
    [str, str, dict[str, str], bytes | None],
    tuple[int, Mapping[str, str], bytes | str | None],
]

#: The job statuses after which polling stops.
TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled"})


class PrismError(Exception):
    """A refused request, carrying Prism's flat ``{detail, code}`` body."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _normalise_base_url(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    for suffix in ("/api/v1", "/api"):
        if base.endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
            break
    return base


def _decode(payload: bytes | str | None) -> Any:
    if payload is None or payload == b"" or payload == "":
        return None
    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            return payload
    else:
        text = payload
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _error_parts(decoded: Any, status: int) -> tuple[str, str]:
    if isinstance(decoded, Mapping):
        detail = decoded.get("detail")
        code = decoded.get("code")
        if detail is not None:
            return str(detail), str(code) if code else f"http-{status}"
    if decoded:
        return str(decoded), f"http-{status}"
    return f"request failed with status {status}", f"http-{status}"


class Client(GeneratedClient):
    """A ``/api/v1`` client for scripts and CI."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        timeout: float = 30,
        transport: Transport | None = None,
    ) -> None:
        resolved = _normalise_base_url(base_url or os.environ.get("PRISM_URL") or "")
        if not resolved:
            raise ValueError("a base URL is required (argument or PRISM_URL)")
        self.base_url = resolved
        self.token = token if token is not None else os.environ.get("PRISM_TOKEN")
        self.timeout = timeout
        self._transport = transport or self._urllib_transport()

    # -- transport -------------------------------------------------------

    def _urllib_transport(self) -> Transport:
        def send(
            method: str,
            url: str,
            headers: dict[str, str],
            body: bytes | None,
        ) -> tuple[int, Mapping[str, str], bytes]:
            request = urllib_request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib_request.urlopen(request, timeout=self.timeout) as response:
                    return response.status, dict(response.headers.items()), response.read()
            except urllib_error.HTTPError as error:
                return error.code, dict((error.headers or {}).items()), error.read()

        return send

    def _request(
        self,
        method: str,
        path: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        body: Any = None,
    ) -> Any:
        """Send one request; generated methods call this and return its JSON."""

        url = self.base_url + path
        for name, value in (path_params or {}).items():
            # Net names may contain "/", which the server's {name:path} route
            # expects to keep; no other path parameter contains one.
            url = url.replace("{" + name + "}", quote(str(value), safe="/"))

        parameters: list[tuple[str, str]] = []
        for name, value in (query or {}).items():
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                parameters.extend((name, str(item)) for item in value)
            else:
                parameters.append((name, str(value)))
        if parameters:
            url += "?" + urlencode(parameters)

        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        payload: bytes | None = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        if method.upper() not in ("GET", "HEAD", "OPTIONS"):
            headers["Idempotency-Key"] = str(uuid.uuid4())

        status, _response_headers, response_body = self._transport(
            method.upper(), url, headers, payload
        )
        decoded = _decode(response_body)
        if status >= 400:
            detail, code = _error_parts(decoded, status)
            raise PrismError(status, code, detail)
        return decoded

    # -- helpers ---------------------------------------------------------

    def iter_events(self, after: int = 0) -> Iterator[dict[str, Any]]:
        """Yield the event log from ``after``, following ``next`` until it ends."""

        cursor = after
        while True:
            page = self.list_events(after=cursor)
            events = (page or {}).get("events") or []
            for event in events:
                yield event
            next_cursor = (page or {}).get("next")
            if not isinstance(next_cursor, int) or next_cursor <= cursor:
                return
            cursor = next_cursor

    def wait_for_job(
        self,
        job_id: str,
        *,
        timeout: float = 600,
        poll: float = 2,
    ) -> dict[str, Any]:
        """Poll ``get_job`` until it reaches a terminal state."""

        deadline = time.monotonic() + timeout
        while True:
            job = self.get_job(job_id)
            if (job or {}).get("status") in TERMINAL_JOB_STATUSES:
                return job
            if time.monotonic() >= deadline:
                raise PrismError(
                    0,
                    "timeout",
                    f"job {job_id} did not finish within {timeout:g} seconds",
                )
            time.sleep(max(float(poll), 0.01))
