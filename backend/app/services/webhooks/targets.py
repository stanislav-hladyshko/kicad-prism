"""Which URLs an outbound webhook may target (ADR §3.10).

A webhook is a server-side request made on an administrator's behalf, so its
target is checked twice: when it is created or edited, and again before every
send. Only ``https`` is accepted, and every address the host resolves to must be
globally routable: private, loopback, link-local, multicast and reserved ranges
are refused, which also closes the IPv6-mapped forms of the IPv4 ranges.

``PRISM_WEBHOOK_ALLOW_PRIVATE`` relaxes both rules (``http`` and private
addresses) for a local receiver during testing; it is off by default.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from app.core.config import settings
from app.core.errors import BadRequest

REFUSED = "webhook-target-refused"
_DEFAULT_PORTS = {"https": 443, "http": 80}
Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def resolve_addresses(host: str, port: int | None) -> list[Address]:
    """Every address ``host`` resolves to. Patchable so tests need no DNS."""

    infos = socket.getaddrinfo(host, port or 0, type=socket.SOCK_STREAM)
    addresses: list[Address] = []
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]
        address = ipaddress.ip_address(raw)
        if address not in addresses:
            addresses.append(address)
    return addresses


def is_public(address: Address) -> bool:
    """Whether one resolved address is outside every range a webhook must not reach."""

    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def require_target(url: str, *, allow_private: bool | None = None) -> str:
    """Return ``url`` unchanged, or raise 400 ``webhook-target-refused``.

    ``allow_private`` defaults to ``PRISM_WEBHOOK_ALLOW_PRIVATE``.
    """

    return _checked(url, allow_private)[0]


def checked_address(url: str, *, allow_private: bool | None = None) -> str:
    """The address a send to ``url`` must connect to, after the same checks.

    Connecting to the address that was checked, rather than resolving the host
    again, keeps a second DNS answer from pointing the request inward.
    """

    return str(_checked(url, allow_private)[1][0])


def _checked(url: str, allow_private: bool | None) -> tuple[str, list[Address]]:
    if allow_private is None:
        allow_private = settings.PRISM_WEBHOOK_ALLOW_PRIVATE
    text = (url or "").strip()
    parsed = urlsplit(text)
    schemes = {"https", "http"} if allow_private else {"https"}
    if parsed.scheme.casefold() not in schemes:
        allowed = "http or https" if allow_private else "https"
        raise BadRequest(f"A webhook URL must use {allowed}.", code=REFUSED)
    host = (parsed.hostname or "").strip()
    if not host:
        raise BadRequest("A webhook URL must name a host.", code=REFUSED)
    if parsed.username or parsed.password:
        raise BadRequest("A webhook URL cannot embed credentials.", code=REFUSED)
    try:
        port = parsed.port or _DEFAULT_PORTS[parsed.scheme.casefold()]
    except ValueError as exc:
        raise BadRequest("A webhook URL has an invalid port.", code=REFUSED) from exc

    addresses = _addresses(host, port)
    if not allow_private and not all(is_public(address) for address in addresses):
        raise BadRequest(
            "A webhook URL must resolve to a public address.", code=REFUSED
        )
    return text, addresses


def _addresses(host: str, port: int) -> list[Address]:
    try:
        addresses = resolve_addresses(host, port)
    except (socket.gaierror, ValueError, UnicodeError) as exc:
        raise BadRequest(
            "A webhook URL's host could not be resolved.", code=REFUSED
        ) from exc
    if not addresses:
        raise BadRequest("A webhook URL's host has no addresses.", code=REFUSED)
    return addresses


__all__ = ["REFUSED", "checked_address", "is_public", "require_target", "resolve_addresses"]
