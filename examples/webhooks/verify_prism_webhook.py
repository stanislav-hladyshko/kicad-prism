"""Verify a Prism outbound webhook delivery (Standard Webhooks).

Prism signs every delivery with HMAC-SHA256 over
``<webhook-id>.<webhook-timestamp>.<body>``. The key is the secret after its
``whsec_`` prefix, base64-decoded. The ``webhook-signature`` header may carry
several space-separated signatures; a delivery is valid when any ``v1`` value
matches. Standard library only, like the SDK.

    from verify_prism_webhook import verify

    event = verify(os.environ["PRISM_WEBHOOK_SECRET"], headers, body)

``verify`` returns the parsed JSON body and raises ``ValueError`` when the
headers are incomplete, the timestamp is stale or in the future, or no
signature matches.

Run this file directly for a tiny receiver:

    PRISM_WEBHOOK_SECRET=whsec_... python verify_prism_webhook.py

It listens on ``127.0.0.1:8080`` (``PRISM_WEBHOOK_PORT`` overrides the port),
prints each accepted event, and answers 400 for an invalid delivery.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Mapping

SECRET_PREFIX = "whsec_"
SIGNATURE_VERSION = "v1"


def _header(headers: Mapping[str, Any], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    raise ValueError(f"missing {name} header")


def _key(secret: str) -> bytes:
    if not secret.startswith(SECRET_PREFIX):
        raise ValueError("a webhook secret starts with whsec_")
    try:
        return base64.b64decode(secret[len(SECRET_PREFIX) :], validate=True)
    except Exception as exc:
        raise ValueError("a webhook secret is base64 after whsec_") from exc


def _body_bytes(body: bytes | str | bytearray) -> bytes:
    if isinstance(body, bytes):
        return body
    if isinstance(body, bytearray):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8")
    raise ValueError("the webhook body must be bytes or str")


def verify(
    secret: str,
    headers: Mapping[str, Any],
    body: bytes | str | bytearray,
    tolerance_seconds: int = 300,
) -> dict[str, Any]:
    """Verify one delivery and return its JSON body.

    ``headers`` is any mapping with case-insensitive access to the Standard
    Webhooks headers (``webhook-id``, ``webhook-timestamp``,
    ``webhook-signature``). Raises ``ValueError`` with a short reason when the
    delivery is not authentic or is outside the timestamp tolerance.
    """

    webhook_id = _header(headers, "webhook-id")
    timestamp_header = _header(headers, "webhook-timestamp")
    signature_header = _header(headers, "webhook-signature")

    try:
        timestamp = int(timestamp_header)
    except (TypeError, ValueError):
        raise ValueError("webhook-timestamp is not an integer") from None

    if abs(time.time() - timestamp) > tolerance_seconds:
        raise ValueError("webhook-timestamp is outside the tolerance")

    payload = _body_bytes(body)
    message = f"{webhook_id}.{timestamp}.".encode("utf-8") + payload
    expected = hmac.new(_key(secret), message, hashlib.sha256).digest()

    for entry in signature_header.split():
        version, separator, value = entry.partition(",")
        if not separator or version != SIGNATURE_VERSION:
            continue
        try:
            candidate = base64.b64decode(value, validate=True)
        except Exception:
            continue
        if hmac.compare_digest(candidate, expected):
            try:
                document = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("the webhook body is not JSON") from exc
            if not isinstance(document, dict):
                raise ValueError("the webhook body is not a JSON object")
            return document

    raise ValueError("webhook-signature does not match")


class _Receiver(BaseHTTPRequestHandler):
    secret: str = ""

    def do_POST(self) -> None:  # noqa: N802 - http.server's spelling
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length)
        try:
            event = verify(self.secret, self.headers, body)
        except ValueError as error:
            self._reply(400, {"error": str(error)})
            return
        print(json.dumps(event, indent=2, sort_keys=True), flush=True)
        self._reply(200, {"received": True})

    def _reply(self, status: int, document: dict[str, Any]) -> None:
        encoded = json.dumps(document).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> int:
    secret = os.environ.get("PRISM_WEBHOOK_SECRET", "")
    if not secret:
        raise SystemExit("set PRISM_WEBHOOK_SECRET to the endpoint's whsec_ secret")
    port = int(os.environ.get("PRISM_WEBHOOK_PORT", "8080"))
    _Receiver.secret = secret
    server = HTTPServer(("127.0.0.1", port), _Receiver)
    print(f"listening on http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
