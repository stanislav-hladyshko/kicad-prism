"""Design Comparison's review queue is one computation with one fixture (ADR §3.8).

`fixtures/review-groups/input.json` is a synthetic comparison result, read here
and by the frontend's `review-groups-fixture.test.ts`; `expected.json` is the
queue both must reproduce. The route test feeds the fixture through a stubbed
result loader and checks the typed response, the not-ready answer and the
unknown job.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from app.api import design_compare_review as api
from app.services.design_query.review_groups import review_groups

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "review-groups"
DOMAINS = ("sch", "pcb")


def _load(name: str):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


class ReviewGroupParityTests(unittest.TestCase):
    def test_python_and_typescript_share_the_fixture_answer(self) -> None:
        result = _load("input.json")
        expected = _load("expected.json")
        for domain in DOMAINS:
            self.assertEqual(review_groups(result, domain), expected[domain], domain)

    def test_an_unknown_domain_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            review_groups(_load("input.json"), "bom")


class ReviewRouteTests(unittest.TestCase):
    def _call(
        self,
        *,
        status,
        result=None,
        domain: str = "sch",
    ):
        with mock.patch.object(
            api, "get_project_for_role_or_404", return_value=SimpleNamespace(id="p1")
        ):
            with mock.patch.object(
                api.design_compare_service, "get_job_status", return_value=status
            ):
                with mock.patch.object(api, "load_comparison_result", return_value=result):
                    return asyncio.run(
                        api.get_comparison_review(
                            "p1", "job-1", domain, SimpleNamespace(role="viewer")
                        )
                    )

    def test_the_route_returns_the_typed_review_queue_for_both_domains(self) -> None:
        result = _load("input.json")
        expected = _load("expected.json")
        for domain in DOMAINS:
            response = self._call(
                status={"project_id": "p1", "status": "completed"},
                result=result,
                domain=domain,
            )
            self.assertIsInstance(response, api.ComparisonReviewOut)
            self.assertEqual(response.jobId, "job-1")
            self.assertEqual(response.domain, domain)
            # The groups are the TypeScript queue's; references are the
            # route's own addition, joined from the changes.
            self.assertEqual(
                [group.model_dump(exclude={"references"}) for group in response.groups],
                expected[domain],
            )

    def test_each_group_names_the_designators_of_its_changes(self) -> None:
        response = self._call(
            status={"project_id": "p1", "status": "completed"},
            result=_load("input.json"),
            domain="sch",
        )
        by_id = {group.id: group.references for group in response.groups}
        self.assertEqual(by_id["schematic:components:cmp:r13"], ["R13"])
        self.assertEqual(by_id["schematic:rules:erc-exclusions"], [])
        part_groups = [refs for gid, refs in by_id.items() if gid.startswith("schematic:components:part>")]
        self.assertTrue(any(len(refs) > 1 for refs in part_groups))

    def test_a_running_job_answers_comparison_not_ready(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            self._call(status={"project_id": "p1", "status": "running"})
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, "comparison-not-ready")

    def test_a_completed_job_without_a_readable_result_is_not_ready(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            self._call(status={"project_id": "p1", "status": "completed"}, result=None)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, "comparison-not-ready")

    def test_an_unknown_job_is_a_404(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            self._call(status=None)
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(raised.exception.detail, "Job not found")

    def test_another_projects_job_is_a_404(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            self._call(status={"project_id": "p2", "status": "completed"})
        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
