"""capture_profile (ADR scripting-and-mcp §11, D26): bounded, exclusive, rate-limited."""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.services import access_service, personal_tokens, workspace_service  # noqa: E402
from app.services.ops import profiler  # noqa: E402
from app.services.postgres_database import database  # noqa: E402

DATABASE_URL = os.environ.get("PRISM_DATABASE_URL", "").strip()


def busy_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        sum(range(200))


class SamplerTests(unittest.TestCase):
    def test_samples_name_modules_and_functions_only(self) -> None:
        stop = threading.Event()
        thread = threading.Thread(target=busy_loop, args=(stop,), name="busy", daemon=True)
        thread.start()
        try:
            result = profiler.sample(0.3, hz=200)
        finally:
            stop.set()
            thread.join()
        self.assertGreater(result["samples"], 10)
        busy = [item for item in result["stacks"] if item["stack"].startswith("busy;")]
        self.assertTrue(busy)
        self.assertIn("test_ops_profiles:busy_loop", busy[0]["stack"])
        self.assertNotIn("/", busy[0]["stack"])

    def test_duration_is_capped(self) -> None:
        with patch.object(profiler.time, "sleep"), patch.object(profiler.time, "monotonic", side_effect=[0.0, 0.0, 100.0]):
            result = profiler.sample(60)
        self.assertEqual(result["seconds"], profiler.MAX_SECONDS)


@unittest.skipUnless(DATABASE_URL, "PRISM_DATABASE_URL is required")
class ProfileStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        workspace_service.workspace.initialize()
        with database.connection() as conn:
            conn.execute("DELETE FROM workspace.ws_ops_profiles")
            conn.commit()

    def test_an_api_profile_is_stored_and_read_back(self) -> None:
        row = profiler.profile_here(0.2, requested_by="a@example.test")
        self.assertEqual(row["status"], "completed")
        self.assertGreater(row["result"]["samples"], 0)
        self.assertEqual(profiler.get(row["id"])["target"], "api")
        self.assertIsNotNone(row["started_at"])
        self.assertLessEqual(row["started_at"], row["finished_at"])

    def test_profiles_are_exclusive(self) -> None:
        with database.connection() as holder:
            holder.execute("SELECT pg_advisory_lock(%s)", (profiler.LOCK_KEY,))
            with self.assertRaises(profiler.ProfileRefused) as raised:
                profiler.profile_here(0.2, requested_by="a")
            holder.execute("SELECT pg_advisory_unlock(%s)", (profiler.LOCK_KEY,))
        self.assertEqual(raised.exception.code, "profile-busy")

    def test_at_most_six_per_hour(self) -> None:
        with database.connection() as conn:
            for _ in range(profiler.MAX_PER_HOUR):
                conn.execute(
                    "INSERT INTO workspace.ws_ops_profiles (id, target, seconds, status) VALUES (%s, 'api', 1, 'completed')",
                    (uuid.uuid4().hex,),
                )
            conn.commit()
        with self.assertRaises(profiler.ProfileRefused) as raised:
            profiler.profile_here(0.2, requested_by="a")
        self.assertEqual(raised.exception.code, "profile-rate-limited")

    def test_a_worker_claims_and_runs_a_requested_profile(self) -> None:
        profile_id = profiler.request_worker(0.2, worker_id=None, requested_by="a")
        claimed = profiler.claim_for_worker("prism:host:1:abc")
        self.assertEqual(claimed["id"], profile_id)
        self.assertIsNone(profiler.claim_for_worker("prism:host:2:def"))
        profiler.run_claimed(claimed)
        row = profiler.wait_for(profile_id, 2)
        self.assertEqual((row["status"], row["worker_id"]), ("completed", "prism:host:1:abc"))

    def test_bad_durations_are_refused(self) -> None:
        for seconds in (0, 11, "x"):
            with self.subTest(seconds=seconds), self.assertRaises(profiler.ProfileRefused):
                profiler.profile_here(seconds, requested_by="a")


try:
    import starlette.testclient  # noqa: F401
    HAS_TEST_CLIENT = True
except RuntimeError:
    HAS_TEST_CLIENT = False


@unittest.skipUnless(DATABASE_URL, "PRISM_DATABASE_URL is required")
@unittest.skipUnless(HAS_TEST_CLIENT, "starlette.testclient needs httpx")
class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from starlette.testclient import TestClient

        cls._patchers = [
            patch.object(settings, "AUTH_ENABLED_OVERRIDE", True),
            patch.object(settings, "PASSWORD_AUTH_ENABLED", True),
            patch.object(settings, "SESSION_SECRET", "test-secret-at-least-32-characters-long-x"),
            patch.object(settings, "PUBLIC_BASE_URL", "https://prism.test"),
            patch.object(settings, "PRISM_MCP_OPS_ENABLED", True),
        ]
        for patcher in cls._patchers:
            patcher.start()
        import app.main as main

        cls.client = TestClient(main.app)

    @classmethod
    def tearDownClass(cls) -> None:
        for patcher in cls._patchers:
            patcher.stop()

    def setUp(self) -> None:
        with database.connection() as conn:
            conn.execute("DELETE FROM workspace.ws_ops_profiles")
            conn.commit()
        email = f"ops-{uuid.uuid4().hex[:8]}@example.test"
        access_service.upsert_user(email=email, name="ops")
        access_service.upsert_user_role(email, "admin", updated_by=email)
        _, token = personal_tokens.create(email, "ops", ["ops:read"], 30)
        self.headers = {"Authorization": f"Bearer {token}"}

    def test_an_api_profile_through_the_route(self) -> None:
        response = self.client.post("/api/v1/ops/profiles", json={"target": "api", "seconds": 0.2}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertTrue(body["stacks"])
        again = self.client.get(f"/api/v1/ops/profiles/{body['id']}", headers=self.headers)
        self.assertEqual(again.json()["id"], body["id"])

    def test_a_worker_profile_no_worker_takes_comes_back_requested(self) -> None:
        with patch("app.api.ops_profiles.WORKER_WAIT_EXTRA_SECONDS", 0.5):
            response = self.client.post(
                "/api/v1/ops/profiles", json={"target": "worker", "seconds": 0.1}, headers=self.headers
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "requested")


if __name__ == "__main__":
    unittest.main()
