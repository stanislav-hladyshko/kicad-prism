"""Signed outbound webhook payloads (Standard Webhooks, ADR §3.10).

Every request carries ``webhook-id`` (the event id), ``webhook-timestamp`` (unix
seconds) and ``webhook-signature``. The signature is ``v1,`` plus the base64
HMAC-SHA256 of ``<id>.<timestamp>.<body>`` keyed by the base64-decoded secret
after its ``whsec_`` prefix, exactly as the Standard Webhooks specification
defines it. The body is compact JSON so the bytes the receiver verifies are the
bytes Prism signed.

Secrets are minted here and nowhere else: ``whsec_`` plus 32 random bytes in
standard base64. A receiver only ever sees the secret in the create or
rotate-secret response.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any, Mapping

SECRET_PREFIX = "whsec_"
SIGNATURE_VERSION = "v1"
_SECRET_BYTES = 32


def generate_secret() -> str:
    """A new signing secret: ``whsec_`` plus 32 random bytes in base64."""

    return SECRET_PREFIX + base64.b64encode(os.urandom(_SECRET_BYTES)).decode("ascii")


def key_bytes(secret: str) -> bytes:
    """The HMAC key a ``whsec_`` secret names."""

    if not secret.startswith(SECRET_PREFIX):
        raise ValueError("a webhook secret starts with whsec_")
    try:
        return base64.b64decode(secret[len(SECRET_PREFIX) :], validate=True)
    except Exception as exc:
        raise ValueError("a webhook secret is base64 after whsec_") from exc


def sign(secret: str, webhook_id: str, body: bytes, timestamp: int) -> str:
    """The ``webhook-signature`` value for one body."""

    message = f"{webhook_id}.{timestamp}.".encode("utf-8") + body
    digest = hmac.new(key_bytes(secret), message, hashlib.sha256).digest()
    return f"{SIGNATURE_VERSION}," + base64.b64encode(digest).decode("ascii")


def headers(secret: str, webhook_id: str, body: bytes, timestamp: int) -> dict[str, str]:
    """The Standard Webhooks headers, plus the JSON content type."""

    return {
        "content-type": "application/json",
        "webhook-id": str(webhook_id),
        "webhook-timestamp": str(int(timestamp)),
        "webhook-signature": sign(secret, str(webhook_id), body, int(timestamp)),
    }


def event_body(event: Mapping[str, Any]) -> bytes:
    """One log event as the compact JSON body Prism signs.

    ``event`` uses the event log's own shape (``id``, ``kind``, ``projectId``,
    ``createdAt``, ``payload``).
    """

    document = {
        "id": event["id"],
        "type": event["kind"],
        "createdAt": event["createdAt"],
        "projectId": event.get("projectId"),
        "data": event.get("payload") or {},
    }
    return _compact(document)


def ping_body(*, event_id: str, created_at: str) -> bytes:
    """The body of a test ping: a ``ping`` event that never entered the log."""

    document = {
        "id": event_id,
        "type": "ping",
        "createdAt": created_at,
        "projectId": None,
        "data": {},
    }
    return _compact(document)


def _compact(document: Mapping[str, Any]) -> bytes:
    return json.dumps(
        document, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


__all__ = [
    "SECRET_PREFIX",
    "SIGNATURE_VERSION",
    "event_body",
    "generate_secret",
    "headers",
    "key_bytes",
    "ping_body",
    "sign",
]
