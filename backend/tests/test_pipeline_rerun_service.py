"""pipeline_rerun_service: the stage row shows what the workflow and its pull request say, and one run per board at a time."""

import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.services import pipeline_rerun_service as reruns
from app.services import pipeline_service

CONFIG = reruns.RerunConfig(repo="owner/boards", workflow="rerun-stage.yml", ref="master", token="t")
NOW = datetime(2026, 10, 28, 12, 30, tzinfo=timezone.utc)
RUNS = "/repos/owner/boards/actions/workflows/rerun-stage.yml/runs"
PULLS = "/repos/owner/boards/pulls"
DISPATCH = "/repos/owner/boards/actions/workflows/rerun-stage.yml/dispatches"


def run(run_id, board, stage, status="completed", started="2026-10-28T12:00:00Z"):
    return {"id": run_id, "display_title": f"rerun {board} stage {stage}", "status": status,
            "run_started_at": started, "html_url": f"https://github.com/owner/boards/actions/runs/{run_id}"}


class FakeGitHub:
    def __init__(self, runs, pulls=None, refuse=None):
        self.runs = runs
        self.pulls = pulls or {}
        self.refuse = refuse
        self.calls = []

    def __call__(self, method, path, params, body):
        self.calls.append((method, path, params, body))
        if self.refuse:
            return self.refuse, {"message": "Resource not accessible by personal access token"}
        if path == RUNS:
            return 200, {"workflow_runs": self.runs}
        if path == PULLS:
            return 200, self.pulls.get(params["head"], [])
        if path == DISPATCH:
            return 204, {}
        raise AssertionError(path)


class StageRuns(unittest.TestCase):
    def test_each_stage_shows_its_latest_run_in_the_words_of_the_feature(self):
        github = FakeGitHub(
            [run(9, "G4", 6, status="in_progress"), run(8, "G4", 7), run(7, "G4", 8), run(6, "G4", 9),
             run(5, "G4", 10, status="queued"), run(4, "G4", 6)],
            pulls={"owner:repair/g4-stage7-8": [{"state": "open", "draft": False, "html_url": "pr/1"}],
                   "owner:repair/g4-stage8-7": [{"state": "open", "draft": True, "html_url": "pr/2"}]},
        )
        stages = reruns.stage_runs("G4", CONFIG, github, now=NOW)
        self.assertEqual({n: s["text"] for n, s in stages.items()}, {
            6: "агент працює, 30 хв",       # the newer run of stage 6 wins over the older one
            7: "PR на огляд",
            8: "не вдалось, PR-чернетка",
            9: "зупинено, без PR",
            10: "у черзі",
        })
        self.assertEqual(stages[7]["url"], "pr/1")

    def test_merged_or_closed_pull_request_leaves_the_row_to_the_verdict(self):
        github = FakeGitHub([run(8, "G4", 6)], pulls={"owner:repair/g4-stage6-8": [{"state": "closed", "draft": False}]})
        self.assertEqual(reruns.stage_runs("G4", CONFIG, github, now=NOW), {})

    def test_runs_of_other_boards_and_other_workflows_are_ignored(self):
        github = FakeGitHub([run(8, "G40", 6, status="queued"), {"id": 1, "display_title": "build", "status": "queued"}])
        self.assertEqual(reruns.stage_runs("G4", CONFIG, github, now=NOW), {})

    def test_refused_token_says_which_permissions_it_needs(self):
        with self.assertRaises(reruns.RerunError) as refused:
            reruns.stage_runs("G4", CONFIG, FakeGitHub([], refuse=403), now=NOW)
        self.assertIn("Actions read/write", str(refused.exception))


class Start(unittest.TestCase):
    def test_dispatches_the_board_and_stage_on_the_configured_branch(self):
        github = FakeGitHub([])
        reruns.start("G4", 6, CONFIG, github)
        self.assertEqual(github.calls[-1], ("POST", DISPATCH, None, {"ref": "master", "inputs": {"board": "G4", "stage": "6"}}))

    def test_second_press_while_a_run_of_the_board_is_going_starts_nothing(self):
        github = FakeGitHub([run(9, "G4", 7, status="in_progress")])
        with self.assertRaises(reruns.RerunError) as refused:
            reruns.start("G4", 6, CONFIG, github)
        self.assertEqual(refused.exception.status_code, 409)
        self.assertNotIn("POST", [method for method, *_ in github.calls])

    def test_stages_that_change_the_circuit_are_refused(self):
        github = FakeGitHub([])
        for stage in (1, 5, 11):
            with self.assertRaises(reruns.RerunError) as refused:
                reruns.start("G4", stage, CONFIG, github)
            self.assertEqual(refused.exception.status_code, 422)
        self.assertEqual(github.calls, [])

    def test_without_repository_or_token_it_is_not_configured(self):
        self.assertFalse(reruns.RerunConfig("owner/boards", "w.yml", "master", "").configured)
        self.assertFalse(reruns.RerunConfig("", "w.yml", "master", "t").configured)


class BoardOfProject(unittest.TestCase):
    def test_project_maps_to_its_pipeline_row_only_when_the_caller_sees_it(self):
        root = "/app/projects/boards"
        files = {str(Path(root) / pipeline_service.PIPELINE_FILE):
                 '{"version": 1, "columns": [], "rows": [{"id": "G4", "dir": "examples/golden/G4", "cells": []}]}'}
        projects = [{"id": "prj_g4", "parent_repo_path": root, "relative_path": "examples/golden/G4"},
                    {"id": "prj_other", "parent_repo_path": root, "relative_path": "examples/other"}]
        read = lambda path: files.get(str(path))  # noqa: E731
        self.assertEqual(pipeline_service.board_id(projects, "prj_g4", read), "G4")
        self.assertIsNone(pipeline_service.board_id(projects, "prj_other", read))
        self.assertIsNone(pipeline_service.board_id(projects, "prj_hidden", read))


if __name__ == "__main__":
    unittest.main()
