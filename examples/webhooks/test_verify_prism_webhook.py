"""The webhook verifier against signatures built by the same algorithm.

The signer is re-implemented here on purpose: the example must not import
anything from ``backend/``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_prism_webhook import verify  # noqa: E402

SECRET = "whsec_" + base64.b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
BODY = json.dumps(
    {"id": "evt_1", "type": "comparison.done", "projectId": "p1", "data": {"jobId": "j1"}},
    separators=(",", ":"),
).encode("utf-8")


def sign(secret: str, webhook_id: str, body: bytes, timestamp: int) -> str:
    key = base64.b64decode(secret[len("whsec_") :])
    message = f"{webhook_id}.{timestamp}.".encode("utf-8") + body
    digest = hmac.new(key, message, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def headers_for(signature: str, timestamp: int, webhook_id: str = "evt_1") -> dict[str, str]:
    return {
        "content-type": "application/json",
        "webhook-id": webhook_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": signature,
    }


class VerifyTests(unittest.TestCase):
    def test_accepts_a_correct_signature(self) -> None:
        timestamp = int(time.time())
        event = verify(
            SECRET,
            headers_for(sign(SECRET, "evt_1", BODY, timestamp), timestamp),
            BODY,
        )
        self.assertEqual(event["id"], "evt_1")
        self.assertEqual(event["data"], {"jobId": "j1"})

    def test_rejects_a_wrong_secret(self) -> None:
        timestamp = int(time.time())
        other = "whsec_" + base64.b64encode(b"another key").decode("ascii")
        with self.assertRaises(ValueError):
            verify(
                other,
                headers_for(sign(SECRET, "evt_1", BODY, timestamp), timestamp),
                BODY,
            )

    def test_rejects_a_tampered_body(self) -> None:
        timestamp = int(time.time())
        headers = headers_for(sign(SECRET, "evt_1", BODY, timestamp), timestamp)
        with self.assertRaises(ValueError):
            verify(SECRET, headers, BODY.replace(b"j1", b"j2"))

    def test_rejects_a_stale_timestamp(self) -> None:
        timestamp = int(time.time()) - 301
        headers = headers_for(sign(SECRET, "evt_1", BODY, timestamp), timestamp)
        with self.assertRaises(ValueError):
            verify(SECRET, headers, BODY)

    def test_accepts_one_of_several_signatures(self) -> None:
        timestamp = int(time.time())
        wrong = sign(SECRET, "evt_2", BODY, timestamp)
        right = sign(SECRET, "evt_1", BODY, timestamp)
        event = verify(
            SECRET,
            headers_for(f"{wrong} {right}", timestamp),
            BODY,
        )
        self.assertEqual(event["type"], "comparison.done")

    def test_rejects_missing_headers(self) -> None:
        with self.assertRaises(ValueError):
            verify(SECRET, {"webhook-id": "evt_1"}, BODY)


if __name__ == "__main__":
    unittest.main()
