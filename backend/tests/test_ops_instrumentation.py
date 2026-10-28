"""Ops instrumentation (ADR scripting-and-mcp §11, D23–D24).

Request ids reach the response and the rows a request writes, database time
follows a request into worker threads, route statistics and counters merge
across flushes and processes, and the request log keeps writes, slow requests
and errors only.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import request_context  # noqa: E402
from app.services import event_log, workspace_service  # noqa: E402
from app.services.ops import stats  # noqa: E402
from app.services.postgres_database import database  # noqa: E402

DATABASE_URL = os.environ.get("PRISM_DATABASE_URL", "").strip()


def reset_stats() -> None:
    with stats._lock:
        stats._routes.clear()
        stats._counters.clear()
        stats._requests.clear()


class RequestContextTests(unittest.TestCase):
    def test_only_well_formed_inbound_ids_are_kept(self) -> None:
        self.assertEqual(request_context.accepted_id("abc-123_x.y"), "abc-123_x.y")
        self.assertIsNone(request_context.accepted_id("has space"))
        self.assertIsNone(request_context.accepted_id("x" * 65))
        self.assertIsNone(request_context.accepted_id(None))

    def test_database_time_in_a_worker_thread_counts_for_the_request(self) -> None:
        async def run() -> float:
            context, token = request_context.begin()
            try:
                await asyncio.to_thread(request_context.add_db_time, 0.25)
                return context.db_seconds
            finally:
                request_context.end(token)

        self.assertAlmostEqual(asyncio.run(run()), 0.25)
        self.assertIsNone(request_context.current())


class AggregationTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_stats()
        self.addCleanup(reset_stats)

    def test_buckets(self) -> None:
        self.assertEqual(stats.bucket_index(0.4), 0)
        self.assertEqual(stats.bucket_index(150), stats.BUCKETS_MS.index(200))
        self.assertEqual(stats.bucket_index(10**6), len(stats.BUCKETS_MS))

    def test_requests_aggregate_per_minute_route_and_status_class(self) -> None:
        now = 1_800_000_000.0
        stats.record_request("get", "/api/projects/{project_id}", 200, 12, now=now)
        stats.record_request("GET", "/api/projects/{project_id}", 404, 3, now=now)
        stats.record_request("GET", "/api/projects/{project_id}", 503, 900, now=now + 1)
        [(key, entry)] = stats._routes.items()
        self.assertEqual(key, (int(now // 60) * 60, "GET", "/api/projects/{project_id}"))
        self.assertEqual(entry[:3], [3, 1, 1])
        self.assertEqual(sum(entry[3]), 3)

    def test_a_failed_flush_keeps_the_data(self) -> None:
        stats.record_request("POST", "/api/x", 201, 5, now=time.time() - 120)
        stats.incr("cache.hit", 2, now=time.time() - 120)
        with patch.object(stats, "_write", side_effect=RuntimeError("db down")):
            stats.flush()
        self.assertEqual(len(stats._routes), 1)
        self.assertEqual(sum(stats._counters.values()), 2)


@unittest.skipUnless(DATABASE_URL, "PRISM_DATABASE_URL is required")
class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        workspace_service.workspace.initialize()
        reset_stats()
        self.addCleanup(reset_stats)
        self.route = f"/api/test/{uuid.uuid4().hex[:8]}"

    def _rows(self, sql: str, params: tuple) -> list[dict]:
        with database.connection() as conn:
            conn.execute("SET search_path TO workspace, public")
            return conn.execute(sql, params).fetchall()

    def test_two_flushes_add_into_one_row(self) -> None:
        past = time.time() - 120
        for _ in range(2):
            stats.record_request("GET", self.route, 200, 7, now=past)
            stats.record_request("GET", self.route, 500, 1500, now=past)
            stats.incr(f"counter{self.route}", 3, now=past)
            stats.flush()
        [row] = self._rows(
            "SELECT count, status_5xx, buckets FROM ws_ops_route_stats WHERE route = %s", (self.route,)
        )
        self.assertEqual((row["count"], row["status_5xx"]), (4, 2))
        self.assertEqual(sum(row["buckets"]), 4)
        [counter] = self._rows("SELECT value FROM ws_ops_counters WHERE name = %s", (f"counter{self.route}",))
        self.assertEqual(counter["value"], 6)

    def test_rows_written_inside_a_request_carry_its_id(self) -> None:
        context, token = request_context.begin()
        try:
            event_id = event_log.record("job.progress", project_id=None, topic="test", payload={})
        finally:
            request_context.end(token)
        [row] = self._rows("SELECT request_id FROM ws_events WHERE id = %s", (event_id,))
        self.assertEqual(row["request_id"], context.request_id)


try:
    import starlette.testclient  # noqa: F401
    HAS_TEST_CLIENT = True
except RuntimeError:
    HAS_TEST_CLIENT = False


@unittest.skipUnless(DATABASE_URL, "PRISM_DATABASE_URL is required")
@unittest.skipUnless(HAS_TEST_CLIENT, "starlette.testclient needs httpx")
class MiddlewareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from starlette.testclient import TestClient

        import app.main as main

        cls.client = TestClient(main.app)

    def setUp(self) -> None:
        reset_stats()
        self.addCleanup(reset_stats)

    def test_every_response_names_its_request(self) -> None:
        made = self.client.get("/api/health/live")
        self.assertRegex(made.headers["x-request-id"], r"^[0-9a-f]{32}$")
        kept = self.client.get("/api/health/live", headers={"X-Request-Id": "trace-abc.1"})
        self.assertEqual(kept.headers["x-request-id"], "trace-abc.1")
        replaced = self.client.get("/api/health/live", headers={"X-Request-Id": "bad id"})
        self.assertNotEqual(replaced.headers["x-request-id"], "bad id")

    def test_fast_reads_are_counted_but_not_logged_and_writes_are_logged(self) -> None:
        self.client.get("/api/health/live")
        self.client.post("/api/health/live")
        routes = {key[2] for key in stats._routes}
        self.assertIn("/api/health/live", routes)
        logged = [row["method"] for row in stats._requests]
        self.assertEqual(logged, ["POST"])
        self.assertEqual(stats._requests[0]["status"], 405)


if __name__ == "__main__":
    unittest.main()
