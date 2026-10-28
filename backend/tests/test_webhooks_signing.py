"""Standard Webhooks signing and payload shape (ADR §3.10).

The known vector is computed here from the specification's own formula with
fixed inputs, so the test fails if either the key decoding, the signed message,
or the base64 encoding drifts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.webhooks import signing  # noqa: E402


def fixed_secret() -> tuple[str, bytes]:
    key = bytes(range(32))
    return "whsec_" + base64.b64encode(key).decode("ascii"), key


class SigningTests(unittest.TestCase):
    def test_a_known_vector_matches_the_specification_formula(self) -> None:
        secret, key = fixed_secret()
        body = b'{"data":{},"id":7,"type":"bundle.built"}'
        webhook_id = "7"
        timestamp = 1_700_000_000

        message = f"{webhook_id}.{timestamp}.".encode("utf-8") + body
        expected = "v1," + base64.b64encode(
            hmac.new(key, message, hashlib.sha256).digest()
        ).decode("ascii")

        self.assertEqual(signing.sign(secret, webhook_id, body, timestamp), expected)

    def test_headers_carry_the_id_timestamp_and_one_signature(self) -> None:
        secret, _key = fixed_secret()
        body = b"{}"
        headers = signing.headers(secret, "42", body, 1_700_000_000)

        self.assertEqual(headers["webhook-id"], "42")
        self.assertEqual(headers["webhook-timestamp"], "1700000000")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(
            headers["webhook-signature"],
            signing.sign(secret, "42", body, 1_700_000_000),
        )
        self.assertTrue(headers["webhook-signature"].startswith("v1,"))

    def test_a_secret_round_trips_through_base64(self) -> None:
        secret = signing.generate_secret()
        self.assertTrue(secret.startswith("whsec_"))
        self.assertEqual(len(signing.key_bytes(secret)), 32)
        self.assertNotEqual(secret, signing.generate_secret())

    def test_a_secret_without_the_prefix_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            signing.key_bytes("not-a-webhook-secret")

    def test_the_event_body_is_compact_json_with_the_contract_keys(self) -> None:
        body = signing.event_body(
            {
                "id": 11,
                "kind": "comparison.done",
                "projectId": "project-a",
                "createdAt": "2026-10-26T12:00:00Z",
                "payload": {"jobId": "job-1"},
            }
        )
        self.assertNotIn(b": ", body)
        self.assertNotIn(b", ", body)
        document = json.loads(body)
        self.assertEqual(
            document,
            {
                "id": 11,
                "type": "comparison.done",
                "createdAt": "2026-10-26T12:00:00Z",
                "projectId": "project-a",
                "data": {"jobId": "job-1"},
            },
        )

    def test_a_ping_is_a_body_with_a_ping_type(self) -> None:
        document = json.loads(
            signing.ping_body(event_id="ping-1", created_at="2026-10-26T12:00:00Z")
        )
        self.assertEqual(document["type"], "ping")
        self.assertEqual(document["id"], "ping-1")
        self.assertIsNone(document["projectId"])
        self.assertEqual(document["data"], {})


if __name__ == "__main__":
    unittest.main()
