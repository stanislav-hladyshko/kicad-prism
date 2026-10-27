"""pipeline_service: rows come from the repository's file, sign-off from Release Studio, nothing invented."""

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.services import pipeline_service
from app.services.pipeline_service import PIPELINE_FILE, Seams

ROOT = "/app/projects/type2/boards"
COMMIT = "a1b2c3d" + "0" * 33


def pipeline_file(rows, version=1):
    return json.dumps({"version": version, "as_of": "2026-10-26", "columns": ["1. Вимоги", "Еталон"], "rows": rows})


def board_row(board_dir, board_id="G4"):
    return {"id": board_id, "dir": board_dir, "board": "USB-UART", "kind": "клас 4", "stop": "6. Розміщення",
            "cells": [{"state": "ok", "text": "так", "why": "verify.json"}, {"state": "bad", "text": "ні", "why": "x"}]}


def project(project_id, relative_path, repo_id="repo_1"):
    return {"id": project_id, "repo_id": repo_id, "parent_repo_path": ROOT, "parent_repo": "boards",
            "relative_path": relative_path}


def approval(at, digest="d1", decision="approved"):
    return {"decision": decision, "dossier_digest": digest, "created_at": at}


class Fake:
    def __init__(self, files=None, candidates=None, builds=None, decisions=None, behind=0):
        self.files = files or {}
        self.candidates = candidates or {}
        self.builds = builds or {}
        self.decisions = decisions or {}
        self.behind = behind
        self.distance_calls = []

    def seams(self):
        return Seams(
            read_file=lambda path: self.files.get(str(path)),
            list_candidates=lambda project_id: self.candidates.get(project_id, []),
            list_builds=lambda candidate_id: self.builds.get(candidate_id, []),
            latest_review_decision=lambda build_id, slot: self.decisions.get((build_id, slot)),
            commit_distance=self.distance,
        )

    def distance(self, repo, commit, sub):
        self.distance_calls.append((repo, commit, sub))
        return self.behind


def signed(behind=0, designer_at=datetime(2026, 10, 20, 9, tzinfo=timezone.utc),
           qa_at=datetime(2026, 10, 21, 15, tzinfo=timezone.utc), qa_digest="d1"):
    return Fake(
        files={str(Path(ROOT) / PIPELINE_FILE): pipeline_file([board_row("examples/golden/G4")])},
        candidates={"prj_g4": [{"id": "cand_1", "commit_sha": COMMIT}]},
        builds={"cand_1": [{"id": "build_1", "status": "succeeded", "dossier_digest": "d1"}]},
        decisions={("build_1", "designer"): approval(designer_at), ("build_1", "qa"): approval(qa_at, digest=qa_digest)},
        behind=behind,
    )


class Pipelines(unittest.TestCase):
    def run_one(self, fake, projects=None):
        result = pipeline_service.pipelines(projects or [project("prj_g4", "examples/golden/G4")], fake.seams())
        return result[0]["rows"][0] if result and result[0]["rows"] else None

    def test_row_carries_the_repository_cells_and_the_prism_project(self):
        row = self.run_one(signed())
        self.assertEqual(row["project_id"], "prj_g4")
        self.assertEqual([c["text"] for c in row["cells"]], ["так", "ні"])
        self.assertEqual(row["stop"], "6. Розміщення")

    def test_both_approvals_on_the_current_revision_read_signed_with_the_later_date(self):
        fake = signed()
        human = self.run_one(fake)["human"]
        self.assertEqual((human["state"], human["text"]), ("ok", "підписано 2026-10-21"))
        self.assertEqual(fake.distance_calls, [(ROOT, COMMIT, "examples/golden/G4")])

    def test_board_changed_after_the_signed_commit_reads_old_revision(self):
        human = self.run_one(signed(behind=2))["human"]
        self.assertEqual((human["state"], human["text"]), ("warn", "підпис на старій ревізії"))
        self.assertIn("2 комітами", human["why"])

    def test_approval_of_another_dossier_is_not_a_signoff(self):
        """release_studio_service._active_approval: a rebuild invalidates the approvals of the old dossier."""
        self.assertEqual(self.run_one(signed(qa_digest="old"))["human"]["text"], "чекає")

    def test_failed_build_is_not_asked_for_approvals(self):
        fake = signed()
        fake.builds["cand_1"][0]["status"] = "failed"
        self.assertEqual(self.run_one(fake)["human"]["text"], "чекає")
        self.assertEqual(fake.distance_calls, [])

    def test_board_without_a_prism_project_is_listed_without_an_id(self):
        row = self.run_one(signed(), projects=[project("prj_other", "examples/ldo33")])
        self.assertIsNone(row["project_id"])
        self.assertEqual(row["human"]["text"], "чекає")
        self.assertIn("не імпортовано", row["human"]["why"])

    def test_repository_without_the_file_has_no_pipeline(self):
        self.assertEqual(pipeline_service.pipelines([project("prj_g4", "examples/golden/G4")], Fake().seams()), [])

    def test_unknown_file_version_is_ignored(self):
        fake = signed()
        fake.files = {str(Path(ROOT) / PIPELINE_FILE): pipeline_file([board_row("examples/golden/G4")], version=2)}
        self.assertEqual(pipeline_service.pipelines([project("prj_g4", "examples/golden/G4")], fake.seams()), [])

    def test_unknown_cell_state_is_not_passed_to_the_page(self):
        fake = signed()
        row = board_row("examples/golden/G4")
        row["cells"][0]["state"] = "<script>"
        fake.files = {str(Path(ROOT) / PIPELINE_FILE): pipeline_file([row])}
        self.assertEqual(self.run_one(fake)["cells"][0]["state"], "na")


if __name__ == "__main__":
    unittest.main()
