"""The /api/v1 operations snapshot: removals, moves, and untyped responses break it."""

from __future__ import annotations

import json
import unittest

from app.core.api_contract import SNAPSHOT, _typed, live, problems

REWRITE = "run `python -m app.core.api_contract --write` from backend/ if the change is deliberate"


def _operation(method: str = "GET", path: str = "/api/v1/x", typed: bool = True) -> dict:
    return {"method": method, "path": path, "typed": typed}


class OperationsSnapshotTests(unittest.TestCase):
    def test_the_live_api_matches_the_snapshot(self) -> None:
        recorded = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        found = problems(live(), recorded)
        self.assertEqual(found, [], f"The live API no longer matches {SNAPSHOT.name}; {REWRITE}.")


class ProblemsTests(unittest.TestCase):
    def test_a_removed_operation_is_a_problem(self) -> None:
        self.assertEqual(
            problems({}, {"read_x": _operation()}),
            ["read_x: removed or renamed (GET /api/v1/x)"],
        )

    def test_a_moved_path_is_a_problem(self) -> None:
        self.assertEqual(
            problems(
                {"read_x": _operation(path="/api/v1/y")},
                {"read_x": _operation(path="/api/v1/x")},
            ),
            ["read_x: moved from GET /api/v1/x to GET /api/v1/y"],
        )

    def test_a_moved_method_is_a_problem(self) -> None:
        self.assertEqual(
            problems(
                {"read_x": _operation(method="POST")},
                {"read_x": _operation(method="GET")},
            ),
            ["read_x: moved from GET /api/v1/x to POST /api/v1/x"],
        )

    def test_a_typed_operation_becoming_untyped_is_a_problem(self) -> None:
        self.assertEqual(
            problems(
                {"read_x": _operation(typed=False)},
                {"read_x": _operation(typed=True)},
            ),
            ["read_x: was typed, is now untyped"],
        )

    def test_a_new_untyped_operation_is_a_problem(self) -> None:
        self.assertEqual(
            problems({"read_y": _operation(path="/api/v1/y", typed=False)}, {}),
            ["read_y: new operation without a typed response (GET /api/v1/y)"],
        )

    def test_a_new_typed_operation_is_fine(self) -> None:
        self.assertEqual(problems({"read_y": _operation(path="/api/v1/y", typed=True)}, {}), [])

    def test_an_unchanged_operation_is_fine(self) -> None:
        recorded = {"read_x": _operation()}
        self.assertEqual(problems(dict(recorded), recorded), [])


class TypedTests(unittest.TestCase):
    def test_a_204_response_is_typed(self) -> None:
        self.assertTrue(_typed({"responses": {"204": {"description": "No content"}}}))

    def test_json_with_a_schema_is_typed(self) -> None:
        self.assertTrue(
            _typed(
                {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {"schema": {"type": "object"}},
                            }
                        }
                    }
                }
            )
        )

    def test_json_with_an_empty_schema_is_untyped(self) -> None:
        self.assertFalse(
            _typed({"responses": {"200": {"content": {"application/json": {"schema": {}}}}}})
        )

    def test_a_non_json_media_type_is_typed(self) -> None:
        self.assertTrue(
            _typed(
                {
                    "responses": {
                        "200": {"content": {"application/octet-stream": {}}}
                    }
                }
            )
        )

    def test_only_an_error_response_is_untyped(self) -> None:
        self.assertFalse(
            _typed({"responses": {"404": {"content": {"application/json": {"schema": {}}}}}})
        )

    def test_a_success_without_content_is_typed(self) -> None:
        self.assertTrue(_typed({"responses": {"200": {"description": "OK"}}}))


if __name__ == "__main__":
    unittest.main()
