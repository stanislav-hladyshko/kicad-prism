"""Client behavior against an injectable transport. Standard library only."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from prism import Client, PrismError  # noqa: E402


def response(data, status: int = 200):
    body = json.dumps(data).encode("utf-8")
    return status, {"content-type": "application/json"}, body


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        return self.responses.pop(0)

    @property
    def call(self):
        return self.calls[-1]


class AuthAndBaseUrlTests(unittest.TestCase):
    def test_bearer_token_and_base_url(self) -> None:
        transport = FakeTransport(response([]))
        client = Client("https://prism.example/", token="secret", transport=transport)
        client.list_projects()
        self.assertEqual(transport.call["url"], "https://prism.example/api/v1/projects/")
        self.assertEqual(transport.call["headers"]["Authorization"], "Bearer secret")

    def test_environment_fallback_and_api_suffix(self) -> None:
        transport = FakeTransport(response([]))
        environment = {"PRISM_URL": "https://env.example/api/v1", "PRISM_TOKEN": "env-token"}
        with patch.dict(os.environ, environment, clear=True):
            client = Client(transport=transport)
            client.list_projects()
        self.assertEqual(client.base_url, "https://env.example")
        self.assertEqual(transport.call["url"], "https://env.example/api/v1/projects/")
        self.assertEqual(transport.call["headers"]["Authorization"], "Bearer env-token")

    def test_no_token_sends_no_authorization(self) -> None:
        transport = FakeTransport(response([]))
        client = Client("https://prism.example", transport=transport)
        client.list_projects()
        self.assertNotIn("Authorization", transport.call["headers"])

    def test_missing_base_url_is_an_error(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                Client(transport=FakeTransport())


class IdempotencyTests(unittest.TestCase):
    def test_get_has_no_idempotency_key(self) -> None:
        transport = FakeTransport(response([]))
        client = Client("https://prism.example", transport=transport)
        client.list_projects()
        self.assertNotIn("Idempotency-Key", transport.call["headers"])

    def test_post_has_a_fresh_idempotency_key(self) -> None:
        transport = FakeTransport(response({}), response({}))
        client = Client("https://prism.example", transport=transport)
        client.sync_project_endpoint("p1")
        first = transport.call["headers"]["Idempotency-Key"]
        client.sync_project_endpoint("p1")
        second = transport.call["headers"]["Idempotency-Key"]
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertNotEqual(first, second)

    def test_a_json_body_is_serialized_and_declared(self) -> None:
        transport = FakeTransport(response({}))
        client = Client("https://prism.example", transport=transport)
        client.analyze_repository(body={"url": "https://example.test/repo.git"})
        self.assertEqual(transport.call["method"], "POST")
        self.assertEqual(transport.call["headers"]["Content-Type"], "application/json")
        self.assertEqual(
            json.loads(transport.call["body"]), {"url": "https://example.test/repo.git"}
        )


class ErrorTests(unittest.TestCase):
    def test_prism_error_parses_detail_and_code(self) -> None:
        transport = FakeTransport(
            (404, {"content-type": "application/json"}, b'{"detail": "no such project", "code": "project-not-found"}')
        )
        client = Client("https://prism.example", transport=transport)
        with self.assertRaises(PrismError) as caught:
            client.get_project_detail("missing")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.code, "project-not-found")
        self.assertEqual(caught.exception.detail, "no such project")
        self.assertEqual(str(caught.exception), "project-not-found: no such project")

    def test_non_json_error_body_still_raises(self) -> None:
        transport = FakeTransport((502, {}, b"upstream unavailable"))
        client = Client("https://prism.example", transport=transport)
        with self.assertRaises(PrismError) as caught:
            client.list_projects()
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(caught.exception.detail, "upstream unavailable")


class PaginationTests(unittest.TestCase):
    def test_iter_events_follows_next(self) -> None:
        transport = FakeTransport(
            response({"events": [{"id": 1}, {"id": 2}], "next": 3}),
            response({"events": [{"id": 3}], "next": 3}),
        )
        client = Client("https://prism.example", transport=transport)
        events = list(client.iter_events(after=1))
        self.assertEqual([event["id"] for event in events], [1, 2, 3])
        self.assertEqual(len(transport.calls), 2)
        self.assertIn("after=1", transport.calls[0]["url"])
        self.assertIn("after=3", transport.calls[1]["url"])


class JobTests(unittest.TestCase):
    def test_wait_for_job_polls_until_terminal(self) -> None:
        transport = FakeTransport(
            response({"job_id": "j1", "status": "running"}),
            response({"job_id": "j1", "status": "completed"}),
        )
        client = Client("https://prism.example", transport=transport)
        job = client.wait_for_job("j1", poll=0.01)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(len(transport.calls), 2)

    def test_wait_for_job_times_out(self) -> None:
        transport = FakeTransport(response({"job_id": "j1", "status": "running"}))
        client = Client("https://prism.example", transport=transport)
        with self.assertRaises(PrismError) as caught:
            client.wait_for_job("j1", timeout=0, poll=0.01)
        self.assertEqual(caught.exception.code, "timeout")


if __name__ == "__main__":
    unittest.main()
