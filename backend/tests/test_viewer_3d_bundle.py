"""The 3D tab from the design bundle (ADR amendment V2 §2, V2a step 3).

``backend/app/services/viewer_3d_bundle.py`` answers the tab's status, the
viewer's analytic document and the members behind it from a revision's stored
bundle. These tests build archives with the ``prism-kicad`` stub's canonical
zip writer at each point of a build (parse only, board stored, parts stored,
board failed) and read them through the service: the readiness the tab shows,
when the viewer may load, what the document names, and which members the asset
route serves. The route tests send status, the document, the members and
generate through the bundle, and refuse any asset outside it.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from app.api import project_viewer_3d as api
from app.core.errors import Conflict
from app.services import viewer_3d_bundle as v3d
from app.services.design_bundle.archive import open_archive
from app.services.design_bundle.generator import bundle_key
from app.services.design_bundle.reader import OpenedRevision
from support import prism_kicad_stub as stub

EXAMPLES = Path(__file__).resolve().parents[2] / "native" / "contracts" / "prism" / "examples"
GENERATOR = json.loads((EXAMPLES / "CliVersionA0.example.json").read_text(encoding="utf-8"))
REVISION_KEY = "a" * 64
GENERATOR_DIGEST = "d" * 64
PROJECT_ID = "prj_demo"
COMMIT = "c" * 40
BUNDLE_KEY = bundle_key(REVISION_KEY, GENERATOR)

PACK_BYTES = b"KMMATPK\0" + b"\0" * 24
GLB_BYTES = b"glTF" + b"\0" * 16
STACKUP = json.loads((EXAMPLES / "StackupA0.example.json").read_text(encoding="utf-8"))
CONFIG = {
    "schema": "prism.revision_key.effective_config.a1",
    "project_file": "demo.kicad_pro",
    "root_schematic": "demo.kicad_sch",
    "board": "demo.kicad_pcb",
    "excluded_dirs": [],
}


def _stage(status: str, **extra) -> dict:
    return {"status": status, "warnings": [], "failed_members": [], **extra}


def write_bundle(path: Path, *, board: str = "pending", models: str = "pending", board_failure: dict | None = None):
    """A stored bundle: parse complete with the stackup projection, then as far as ``board`` and ``models``."""

    stackup = (json.dumps(STACKUP) + "\n").encode("utf-8")
    listed = [{"path": v3d.STACKUP, "stage": "parse", "role": "projection", "sha256": stub.digest(stackup), "bytes": len(stackup),
               "member_schema": next(s["schema"] for s in GENERATOR["member_schemas"] if s["path"] == v3d.STACKUP)}]
    members = {v3d.STACKUP: stackup}
    board_record = _stage(board)
    if board == "complete":
        listed.append({"path": v3d.PACK, "stage": "board", "role": "materialization", "sha256": stub.digest(PACK_BYTES),
                       "bytes": len(PACK_BYTES), "member_schema": "kicad_monkey.materialization.a0"})
        members[v3d.PACK] = PACK_BYTES
    if board_failure:
        board_record = _stage("partial", failed_members=[board_failure])
    if models == "complete":
        listed.append({"path": v3d.COMPONENTS, "stage": "models", "role": "component-models", "sha256": stub.digest(GLB_BYTES),
                       "bytes": len(GLB_BYTES)})
        members[v3d.COMPONENTS] = GLB_BYTES
    listed.sort(key=lambda member: member["path"].encode("utf-8"))
    manifest = {
        "schema": "prism.bundle.manifest.a3",
        "generator": GENERATOR,
        "revision_key": REVISION_KEY,
        "bundle_key": BUNDLE_KEY,
        "config": CONFIG,
        "board_id": "main",
        "stages": {"parse": _stage("complete"), "board": board_record, "models": _stage(models)},
        "read_set": {"probes": [], "listings": [], "gitlinks": []},
        "members": listed,
    }
    entries = [("bundle.json", stub.compact(manifest) + b"\n")] + [(m["path"], members[m["path"]]) for m in listed]
    path.write_bytes(stub.zip_bytes(entries))
    return open_archive(path)


class ViewerBundleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        generator = SimpleNamespace(document=GENERATOR, digest=GENERATOR_DIGEST)
        self.enterContext(mock.patch.object(v3d.cli, "generator", return_value=generator))
        self.enterContext(mock.patch.object(v3d.rows, "get_bundle", return_value={"job_id": "job_7"}))
        # Reading a member marks the bundle used (retention); no database here.
        self.enterContext(mock.patch.object(v3d.reader.rows, "touch_bundle"))

    def opened(self, archive=None, *, active: bool = False, absent: bool = False) -> OpenedRevision:
        return OpenedRevision(PROJECT_ID, COMMIT, None if absent else REVISION_KEY, GENERATOR_DIGEST,
                              None if absent else BUNDLE_KEY, archive, active, absent=absent)

    def serve(self, opened: OpenedRevision):
        self.enterContext(mock.patch.object(v3d.reader, "open_revision", return_value=opened))
        self.enterContext(mock.patch.object(v3d.reader, "open_revision_key", return_value=opened))

    def test_a_cold_revision_waits_for_its_board_with_the_queued_job(self) -> None:
        opener = mock.patch.object(v3d.reader, "open_revision", return_value=self.opened(active=True))
        with opener as called:
            answer = v3d.status(PROJECT_ID, COMMIT, requested_by="dev@example.com")
            self.assertIsNone(v3d.document(PROJECT_ID, COMMIT))
        called.assert_any_call(PROJECT_ID, COMMIT, queue=True, requested_by="dev@example.com")
        self.assertEqual(answer["status"], "building")
        self.assertFalse(answer["available"])
        self.assertNotIn("bundle_url", answer)
        self.assertEqual(answer["job_id"], "job_7")
        self.assertEqual(answer["readiness"]["stage"], "generating")
        self.assertIn("stage board", answer["message"])

    def test_the_board_shows_once_stored_and_the_parts_join_later(self) -> None:
        self.serve(self.opened(write_bundle(self.tmp / "b.prism", board="complete"), active=True))
        answer = v3d.status(PROJECT_ID, COMMIT)
        self.assertEqual((answer["status"], answer["available"]), ("ready", True))
        self.assertEqual(answer["readiness"]["stage"], "board-ready")
        self.assertEqual(answer["readiness"]["revision"], f"{BUNDLE_KEY}:board-ready")
        self.assertEqual(answer["bundle_url"], f"/api/projects/{PROJECT_ID}/webgpu-3d/analytic?commit={COMMIT}")
        self.assertEqual(sorted(answer["readiness"]["available_assets"]), ["analytic_pack", "stackup"])
        document = v3d.document(PROJECT_ID, COMMIT)
        self.assertEqual(document["schema"], "prism.viewer_3d.analytic.a0")
        self.assertEqual(document["cache_key"], BUNDLE_KEY)
        self.assertEqual(document["assets"], {"analytic_pack": v3d.PACK, "stackup": v3d.STACKUP})
        self.assertEqual(document["readiness"]["stage"], "board-ready")
        self.assertEqual(
            document["asset_base"],
            f"/api/projects/{PROJECT_ID}/webgpu-3d/assets/{REVISION_KEY}/{GENERATOR_DIGEST}/design-bundle/",
        )

    def test_a_finished_bundle_is_final_with_its_parts(self) -> None:
        self.serve(self.opened(write_bundle(self.tmp / "b.prism", board="complete", models="complete")))
        answer = v3d.status(PROJECT_ID, COMMIT)
        self.assertEqual((answer["readiness"]["stage"], answer["readiness"]["progress"]), ("semantic-ready", 100))
        self.assertIsNone(answer["job_id"])
        document = v3d.document(PROJECT_ID, COMMIT)
        self.assertEqual(document["assets"]["components_glb"], v3d.COMPONENTS)
        self.assertEqual(document["readiness"], {"stage": "semantic-ready", "progress": 100})

    def test_a_failed_board_is_an_error_not_a_mesh(self) -> None:
        failure = {"path": v3d.PACK, "code": "materialization-failed", "message": "zone outline open"}
        self.serve(self.opened(write_bundle(self.tmp / "b.prism", board_failure=failure)))
        answer = v3d.status(PROJECT_ID, COMMIT)
        self.assertEqual((answer["status"], answer["available"]), ("invalid", False))
        self.assertIn("materialization-failed: zone outline open", answer["error"])
        self.assertIsNone(v3d.document(PROJECT_ID, COMMIT))

    def test_a_build_that_ended_failed_stops_waiting(self) -> None:
        # No archive and a failed, unretried build: an error, so the tab stops polling.
        opened = self.opened()
        opened.failure = "prism-kicad stage parse: exit 3"
        self.serve(opened)
        answer = v3d.status(PROJECT_ID, COMMIT)
        self.assertEqual((answer["status"], answer["available"]), ("invalid", False))
        self.assertIn("prism-kicad stage parse: exit 3", answer["error"])
        self.assertIn("Regenerate", answer["error"])
        # A stored board whose parts failed is final: the board shows and polling stops.
        opened = self.opened(write_bundle(self.tmp / "b.prism", board="complete"))
        opened.failure = "glb-export-failed"
        self.serve(opened)
        answer = v3d.status(PROJECT_ID, COMMIT)
        self.assertEqual((answer["status"], answer["readiness"]["stage"]), ("ready", "semantic-ready"))
        self.assertIn("parts could not be exported", answer["message"])

    def test_a_revision_without_a_board_is_missing(self) -> None:
        self.serve(self.opened(absent=True))
        answer = v3d.status(PROJECT_ID, COMMIT)
        self.assertEqual((answer["status"], answer["available"]), ("missing", False))
        self.assertIsNone(v3d.document(PROJECT_ID, COMMIT))

    def test_status_and_document_match_the_response_models(self) -> None:
        """Every status state and the analytic document serialize unchanged."""

        from fastapi.encoders import jsonable_encoder

        from app.api import project_viewer_3d_models as models

        failure = {"path": v3d.PACK, "code": "materialization-failed", "message": "zone outline open"}
        for opened in (
            self.opened(active=True),
            self.opened(write_bundle(self.tmp / "b1.prism", board="complete")),
            self.opened(write_bundle(self.tmp / "b2.prism", board="complete", models="complete")),
            self.opened(write_bundle(self.tmp / "b3.prism", board_failure=failure)),
            self.opened(absent=True),
        ):
            self.serve(opened)
            answer = v3d.status(PROJECT_ID, COMMIT)
            typed = models.WebGpu3dStatus.model_validate(answer).model_dump(
                mode="json", by_alias=True, exclude_unset=True
            )
            self.assertEqual(typed, jsonable_encoder(answer))
            document = v3d.document(PROJECT_ID, COMMIT)
            if document is not None:
                typed_document = models.WebGpu3dAnalytic.model_validate(document).model_dump(
                    mode="json", by_alias=True, exclude_unset=True
                )
                self.assertEqual(typed_document, jsonable_encoder(document))

    def test_only_the_three_members_are_served_and_only_when_stored(self) -> None:
        self.serve(self.opened(write_bundle(self.tmp / "b.prism", board="complete")))
        self.assertEqual(v3d.member(PROJECT_ID, REVISION_KEY, GENERATOR_DIGEST, v3d.PACK), (PACK_BYTES, "application/octet-stream"))
        data, media = v3d.member(PROJECT_ID, REVISION_KEY, GENERATOR_DIGEST, v3d.STACKUP)
        self.assertEqual((json.loads(data), media), (STACKUP, "application/json"))
        self.assertIsNone(v3d.member(PROJECT_ID, REVISION_KEY, GENERATOR_DIGEST, v3d.COMPONENTS), "not stored yet")
        self.assertIsNone(v3d.member(PROJECT_ID, REVISION_KEY, GENERATOR_DIGEST, "design.json"), "not a 3D member")
        self.assertIsNone(v3d.member(PROJECT_ID, REVISION_KEY, GENERATOR_DIGEST, "../bundle.json"))

    def test_another_generators_or_an_unknown_revisions_member_is_not_served(self) -> None:
        with mock.patch.object(v3d.reader, "open_revision_key", return_value=None):
            self.assertIsNone(v3d.member(PROJECT_ID, REVISION_KEY, "e" * 64, v3d.PACK))



VIEWER = SimpleNamespace(email="viewer@example.com", role="viewer")


class RouteTest(unittest.TestCase):
    """The routes serve the analytic model; there is no mesh path behind them."""

    def setUp(self) -> None:
        self.enterContext(mock.patch.object(api, "get_project_for_role_or_404"))

    def test_an_asset_outside_the_bundle_is_not_served(self) -> None:
        with mock.patch.object(v3d, "member") as member, self.assertRaises(HTTPException) as raised:
            asyncio.run(api.get_webgpu_3d_asset(PROJECT_ID, REVISION_KEY, GENERATOR_DIGEST, "bundle.json", VIEWER))
        self.assertEqual(raised.exception.status_code, 404)
        member.assert_not_called()

    def test_status_document_and_members_come_from_the_bundle(self) -> None:
        with mock.patch.object(v3d, "status", return_value={"status": "building"}) as status:
            self.assertEqual(asyncio.run(api.get_webgpu_3d_status(PROJECT_ID, COMMIT, VIEWER)), {"status": "building"})
        status.assert_called_once_with(PROJECT_ID, COMMIT, requested_by=VIEWER.email)
        document = {"schema": v3d.DOCUMENT_SCHEMA}
        with mock.patch.object(v3d, "document", return_value=document):
            response = asyncio.run(api.get_webgpu_3d_analytic(PROJECT_ID, COMMIT, VIEWER))
        self.assertEqual(json.loads(response.body), document)
        self.assertEqual(response.headers["cache-control"], "no-store")
        with mock.patch.object(v3d, "member", return_value=(PACK_BYTES, "application/octet-stream")) as member:
            response = asyncio.run(api.get_webgpu_3d_asset(PROJECT_ID, REVISION_KEY, GENERATOR_DIGEST, f"design-bundle/{v3d.PACK}", VIEWER))
        member.assert_called_once_with(PROJECT_ID, REVISION_KEY, GENERATOR_DIGEST, v3d.PACK)
        self.assertEqual(response.body, PACK_BYTES)
        self.assertIn("immutable", response.headers["cache-control"])
        with mock.patch.object(v3d, "document", return_value=None), self.assertRaises(HTTPException) as raised:
            asyncio.run(api.get_webgpu_3d_analytic(PROJECT_ID, COMMIT, VIEWER))
        self.assertEqual(raised.exception.status_code, 404)

    def test_generate_queues_the_bundle_and_force_rebuilds_it(self) -> None:
        with mock.patch.object(api.bundle_jobs, "start_design_bundle_job", return_value="job_1") as start:
            answer = asyncio.run(api.generate_webgpu_3d(PROJECT_ID, api.WebGpu3dGenerateRequest(commit=COMMIT), VIEWER))
        self.assertEqual(answer, {"job_id": "job_1"})
        start.assert_called_once_with(PROJECT_ID, commit=COMMIT, requested_by=VIEWER.email, retry_failed=True)
        done = SimpleNamespace(job_id="job_2")
        with mock.patch.object(api.regenerate, "regenerate", return_value=done) as rebuild:
            answer = asyncio.run(api.generate_webgpu_3d(PROJECT_ID, api.WebGpu3dGenerateRequest(commit=COMMIT, force=True), VIEWER))
        self.assertEqual(answer, {"job_id": "job_2"})
        rebuild.assert_called_once_with(PROJECT_ID, commit=COMMIT, requested_by=VIEWER.email)
        conflict = api.regenerate.RegenerateConflict("busy")
        # The app's handler answers a Conflict with 409 and the service's own text.
        with mock.patch.object(api.regenerate, "regenerate", side_effect=conflict), self.assertRaises(Conflict):
            asyncio.run(api.generate_webgpu_3d(PROJECT_ID, api.WebGpu3dGenerateRequest(force=True), VIEWER))


if __name__ == "__main__":
    unittest.main()
