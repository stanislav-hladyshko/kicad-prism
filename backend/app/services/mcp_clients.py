"""Client ID Metadata Documents: how an MCP client identifies itself (ADR §3.6).

An MCP client has no registration step. Its ``client_id`` is an ``https://`` URL
that serves a small JSON document naming the client and the redirect URIs it may
be sent back to. Prism fetches that document, caches it briefly, and requires
every request's ``redirect_uri`` to be listed in it.

Fetching a URL supplied by an unauthenticated caller is a server-side request
forgery risk, so the host is resolved once, every address must be globally
routable, and the connection goes to the checked address (TLS still verifies the
real hostname), so a second DNS answer cannot point the fetch inward.
A document is also size-capped (64 KiB), must be ``application/json``, and may
not redirect.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse

import urllib3

DOCUMENT_CACHE_SECONDS = 60 * 60
FETCH_TIMEOUT_SECONDS = 5
MAX_DOCUMENT_BYTES = 64 * 1024
MAX_CLIENT_NAME_LENGTH = 120

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


class McpClientError(Exception):
    """A client-document or redirect problem, phrased as an OAuth error."""

    def __init__(self, error: str, description: str, *, status_code: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status_code = status_code


# Injectable seams: tests replace these rather than reaching the network.
_FETCHER: Callable[[str, str], tuple[int, str, bytes]] = lambda url, address: _pinned_fetch(url, address)
_RESOLVER: Callable[..., Any] = socket.getaddrinfo

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _pinned_fetch(url: str, address: str) -> tuple[int, str, bytes]:
    """GET ``url`` from ``address``, verifying TLS against the URL's own host."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    pool = urllib3.HTTPSConnectionPool(
        address,
        port=parsed.port or 443,
        server_hostname=host,
        assert_hostname=host,
        cert_reqs="CERT_REQUIRED",
        timeout=urllib3.Timeout(total=FETCH_TIMEOUT_SECONDS),
        retries=False,
    )
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    response = pool.request(
        "GET",
        path,
        headers={"Host": parsed.netloc, "Accept": "application/json"},
        redirect=False,
        preload_content=False,
    )
    try:
        body = bytearray()
        for chunk in response.stream(4096):
            body.extend(chunk)
            if len(body) > MAX_DOCUMENT_BYTES:
                break
        return response.status, response.headers.get("content-type", ""), bytes(body)
    finally:
        response.release_conn()
        pool.close()


def host_of(client_id: str) -> str:
    """The client URL's host, for display beside its name."""
    return (urlparse(client_id).hostname or "").lower()


def client_id_is_url(client_id: str) -> bool:
    parsed = urlparse(client_id or "")
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.path.strip("/") != ""
    )


def _is_public(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


def _ensure_public_host(host: str, port: int = 443, *, resolver: Callable[..., Any] | None = None) -> str:
    """Refuse a host that resolves anywhere but the public internet.

    Returns the checked address the fetch must connect to.
    """
    if not host:
        raise McpClientError("invalid_client", "client_id has no host")
    resolve = resolver or _RESOLVER
    try:
        infos = resolve(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise McpClientError("invalid_client", "Could not resolve the client host") from exc
    addresses = sorted({str(info[4][0]) for info in infos if len(info) >= 5})
    if not addresses:
        raise McpClientError("invalid_client", "Could not resolve the client host")
    if not all(_is_public(address) for address in addresses):
        raise McpClientError("invalid_client", "client_id resolves to a private address")
    return addresses[0]


def _parse_redirect_uri(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise McpClientError("invalid_client", "redirect_uris must be non-empty strings")
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and host:
        return value
    if parsed.scheme == "http" and host in _LOOPBACK_HOSTS:
        return value
    raise McpClientError("invalid_client", "redirect_uris must be https or a loopback URL")


def validate_document(client_id: str, document: object) -> dict[str, Any]:
    """Check one CIMD payload and return its normalised form."""
    if not isinstance(document, dict):
        raise McpClientError("invalid_client", "Client metadata is not a JSON object")
    if document.get("client_id") != client_id:
        raise McpClientError("invalid_client", "client_id does not match the document")
    name = document.get("client_name")
    if not isinstance(name, str) or len(name) > MAX_CLIENT_NAME_LENGTH:
        raise McpClientError("invalid_client", "client_name must be a string of at most 120 characters")
    redirect_uris = document.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise McpClientError("invalid_client", "redirect_uris must be a non-empty list")
    parsed_uris = [_parse_redirect_uri(uri) for uri in redirect_uris]
    return {
        "client_id": client_id,
        "client_name": name.strip() or host_of(client_id),
        "redirect_uris": parsed_uris,
    }


def _fetch_document(
    client_id: str,
    address: str,
    *,
    fetcher: Callable[[str, str], tuple[int, str, bytes]] | None = None,
) -> dict[str, Any]:
    fetch = fetcher or _FETCHER
    status, content_type, body = fetch(client_id, address)
    if status != 200:
        raise McpClientError("invalid_client", "Could not fetch the client metadata document")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise McpClientError("invalid_client", "Client metadata must be application/json")
    if len(body) > MAX_DOCUMENT_BYTES:
        raise McpClientError("invalid_client", "Client metadata document is too large")
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise McpClientError("invalid_client", "Client metadata is not valid JSON") from exc
    return validate_document(client_id, document)


def fetch_document(
    client_id: str,
    *,
    resolver: Callable[..., Any] | None = None,
    fetcher: Callable[[str, str], tuple[int, str, bytes]] | None = None,
) -> dict[str, Any]:
    """The validated metadata document for ``client_id``, cached for an hour."""
    if not client_id_is_url(client_id):
        raise McpClientError("invalid_client", "client_id must be an https URL with a path")

    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(client_id)
        if cached and cached[0] > now:
            return cached[1]

    address = _ensure_public_host(
        host_of(client_id), urlparse(client_id).port or 443, resolver=resolver
    )
    document = _fetch_document(client_id, address, fetcher=fetcher)
    with _cache_lock:
        _cache[client_id] = (now + DOCUMENT_CACHE_SECONDS, document)
    return document


def _is_loopback(parsed: Any) -> bool:
    return parsed.scheme == "http" and (parsed.hostname or "").lower() in _LOOPBACK_HOSTS


def _loopback_key(parsed: Any) -> tuple[str, str, str, str, str, str]:
    """A loopback URI without its port, so any local port may be used (RFC 8252)."""
    return (
        parsed.scheme,
        (parsed.hostname or "").lower(),
        parsed.path,
        parsed.params,
        parsed.query,
        parsed.fragment,
    )


def redirect_uri_allowed(document: dict[str, Any], redirect_uri: str) -> bool:
    """Whether a request's redirect URI exactly matches one the client listed."""
    requested = urlparse(redirect_uri or "")
    requested_loopback = _is_loopback(requested)
    if not requested_loopback and requested.scheme != "https":
        return False
    if not requested.hostname:
        return False
    listed = [urlparse(uri) for uri in document.get("redirect_uris") or []]
    for candidate in listed:
        if redirect_uri == candidate.geturl():
            return True
        if requested_loopback and _is_loopback(candidate) and _loopback_key(requested) == _loopback_key(candidate):
            return True
    return False
