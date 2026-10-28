"""Release Studio publish: approve -> package -> forge -> record, with a fake forge.

``ReleasePublisher`` is driven through its interface with in-memory records and
a fake forge (``tests/release_publish_fakes.py``). The event-loop tests are the
only ones that go through the routes: that is where the blocking used to be.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.api import release_studio as api
from app.core.errors import DomainError
from app.release_studio.config import technical_config_digest
from app.services import event_log
from app.services.forge_publish_service import ForgePublishError
from app.services.job_service import jobs as job_queue
from app.services.release_studio_publish_service import ReleasePublisher
from app.services.workspace_schema_migrations import WORKSPACE_LEDGER
from support.postgres import requires_scratch_postgres, scratch_schema
from tests.release_publish_fakes import FakeForge, FakeRecords

REPO = {"name": "board", "repo_url": "https://github.com/org/board.git"}
DESIGNER = SimpleNamespace(email="designer@example.com", role="designer")
QA = SimpleNamespace(email="qa@example.com", role="qa")


def _dossier(directory: Path) -> Path:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        data = b"%PDF-1.4 cover"
        info = tarfile.TarInfo("documentation/cover.pdf")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    path = directory / "dossier.tar.gz"
    path.write_bytes(payload.getvalue())
    return path


class _Desk(unittest.TestCase):
    """One project with one succeeded, approved build composed as v1.0.0."""

    configuration = {"revision": "v1.0.0", "release_notes": "composed notes", "vendors": []}

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.records = FakeRecords(
            builds=[
                {
                    "id": "build-1",
                    "candidate_id": "candidate-1",
                    "status": "succeeded",
                    "dossier_artifact_id": "dossier-1",
                    "evidence_artifact_id": "",
                    "dossier_digest": "d" * 64,
                }
            ],
            candidates=[
                {
                    "id": "candidate-1",
                    "project_id": "project",
                    "commit_sha": "a" * 40,
                    "config_key": "release",
                    "created_by": "designer@example.com",
                }
            ],
            artifacts={"dossier-1": _dossier(Path(tmp.name))},
        )
        self.records.approve("build-1", "d" * 64)
        self.forge = FakeForge()
        self.publisher = ReleasePublisher(
            records=self.records,
            forges=self.forge,
            configuration=lambda _project_id, _candidate: dict(self.configuration),
        )

    def publish(self, tag: str = "v1.0.0", notes: str = "browser notes") -> dict:
        return self.publisher.publish("project", REPO, "build-1", tag=tag, notes=notes, actor=QA)


class PublishTests(_Desk):
    def test_publish_zips_the_dossier_creates_the_release_and_records_it(self) -> None:
        result = self.publish()

        self.assertEqual(result["filename"], "board-v1.0.0.zip")
        self.assertEqual(result["release"]["url"], "https://github.com/org/board/releases/tag/v1.0.0")
        release = self.forge.releases_by_tag["v1.0.0"]
        self.assertEqual(release["commit_sha"], "a" * 40)
        self.assertEqual(release["assets"], ["board-v1.0.0.zip"])
        # Notes reach the cover's revision-history table, so the Release
        # description is the composed text rather than the browser's copy.
        self.assertEqual(release["notes"], "composed notes")
        record = self.records.get_publish_record("build-1")
        self.assertEqual(record["tag"], "v1.0.0")
        self.assertEqual(record["asset_names"], ["board-v1.0.0.zip"])
        self.assertEqual(record["published_by"], "qa@example.com")

    def test_a_tag_the_build_was_not_composed_as_is_refused(self) -> None:
        """A stale draft must not name a Release the sheets inside contradict."""

        with self.assertRaises(DomainError) as caught:
            self.publish(tag="v1.2.0")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("v1.0.0", str(caught.exception.detail))
        self.assertNotIn("create_release", self.forge.calls)

    def test_a_build_that_recorded_no_tag_is_refused(self) -> None:
        self.configuration = {}
        with self.assertRaises(DomainError) as caught:
            self.publish()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertNotIn("create_release", self.forge.calls)

    def test_a_failed_build_is_refused(self) -> None:
        self.records.builds["build-1"]["status"] = "failed"
        with self.assertRaises(DomainError) as caught:
            self.publish()
        self.assertEqual(caught.exception.status_code, 409)

    def test_a_build_without_both_approvals_is_refused_with_the_reason(self) -> None:
        self.records.decisions.clear()
        with self.assertRaises(DomainError) as caught:
            self.publish()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("Designer and QA", str(caught.exception.detail))
        self.assertNotIn("create_release", self.forge.calls)

    def test_a_published_build_is_not_published_twice(self) -> None:
        self.publish()
        with self.assertRaises(DomainError) as caught:
            self.publish()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.forge.calls.count("create_release"), 1)

    def test_a_failed_record_does_not_leave_a_release_that_blocks_the_retry(self) -> None:
        """The forge Release used to stay up with no local record; the retry hit "already exists"."""

        self.records.record_failure = RuntimeError("database went away")
        with self.assertRaises(RuntimeError):
            self.publish()
        self.assertEqual(self.forge.releases_by_tag, {}, "the forge kept a Release Prism has no record of")
        self.assertIsNone(self.records.get_publish_record("build-1"))

        retried = self.publish()

        self.assertEqual(retried["release"]["tag"], "v1.0.0")
        self.assertEqual(list(self.forge.releases_by_tag), ["v1.0.0"])
        self.assertIsNotNone(self.records.get_publish_record("build-1"))

    def test_a_failed_record_also_deletes_the_tag_the_publish_created(self) -> None:
        """D32: the tag left behind used to refuse a new build with that tag at enqueue."""

        self.records.record_failure = RuntimeError("database went away")
        with self.assertRaises(RuntimeError):
            self.publish()
        self.assertNotIn("v1.0.0", self.forge.tags)
        identity = {"tag": "v1.0.0", "document_name": "DOC", "date": "2026-10-11"}
        self.publisher.require_new_tag(REPO, identity)

    def test_a_failed_record_keeps_a_tag_that_was_there_before_the_publish(self) -> None:
        self.forge.tags.add("v1.0.0")
        self.records.record_failure = RuntimeError("database went away")
        with self.assertRaises(RuntimeError):
            self.publish()
        self.assertEqual(self.forge.releases_by_tag, {})
        self.assertIn("v1.0.0", self.forge.tags)

    def test_a_tag_the_forge_cannot_check_is_kept_on_rollback(self) -> None:
        def unanswerable(_tag: str) -> bool:
            raise ForgePublishError("GitHub is down", status_code=502)

        self.forge.tag_exists = unanswerable  # type: ignore[method-assign]
        self.records.record_failure = RuntimeError("database went away")
        with self.assertRaises(RuntimeError):
            self.publish()
        self.assertEqual(self.forge.releases_by_tag, {})
        self.assertIn("v1.0.0", self.forge.tags)

    def test_a_vendor_pack_that_is_not_ready_names_the_vendor_not_the_parser_error(self) -> None:
        self.configuration = {**self.configuration, "vendors": ["jlcpcb"]}
        with patch(
            "app.release_studio.vendors.vendor_pack_readiness",
            return_value={"vendor_id": "jlcpcb", "ready": True},
        ):
            with self.assertRaises(DomainError) as caught:
                self.publish()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail, "Selected vendor pack jlcpcb is not ready.")
        self.assertNotIn("create_release", self.forge.calls)


class DetailAndApprovalTests(_Desk):
    def test_detail_joins_the_live_forge_release_by_tag(self) -> None:
        self.publish()
        detail = self.publisher.detail("project", REPO, "build-1", QA)

        self.assertEqual(
            detail["forge_release"],
            {"tag": "v1.0.0", "url": "https://github.com/org/board/releases/tag/v1.0.0"},
        )
        self.assertEqual(detail["approvals"]["published"]["tag"], "v1.0.0")
        self.assertEqual(detail["forge"]["kind"], "github")

    def test_a_release_deleted_on_the_forge_reads_as_not_live(self) -> None:
        self.publish()
        self.forge.releases_by_tag.clear()
        self.assertIsNone(self.publisher.detail("project", REPO, "build-1", QA)["forge_release"])

    def test_a_build_of_another_project_is_not_found(self) -> None:
        with self.assertRaises(DomainError) as caught:
            self.publisher.detail("other-project", REPO, "build-1", QA)
        self.assertEqual(caught.exception.status_code, 404)

    def test_withdrawing_an_approval_blocks_publish(self) -> None:
        state = self.publisher.decide(
            "project", "build-1", slot="qa", decision="withdrawn", note="", actor=QA
        )
        self.assertFalse(state["approvals"]["can_publish"])
        self.assertEqual(self.records.decisions[-1]["decision"], "withdrawn")
        self.assertEqual(self.records.decisions[-1]["actor"], "qa@example.com")


@requires_scratch_postgres("the release.approved producer")
class ApprovalEventTests(_Desk):
    def test_an_approval_appends_release_approved_after_the_decision_commits(self) -> None:
        self.records.decisions.clear()  # _Desk starts fully approved; cast the QA slot here.
        events = scratch_schema(self, "release_events", ledgers=(WORKSPACE_LEDGER,))

        with patch.object(event_log, "connect", events.connection):
            self.publisher.decide(
                "project", "build-1", slot="qa", decision="approved", note="", actor=QA
            )

        with events.connect() as conn:
            rows = conn.execute("SELECT kind, project_id, payload FROM ws_events").fetchall()
        self.assertEqual(
            [(row["kind"], row["project_id"]) for row in rows],
            [("release.approved", "project")],
        )
        self.assertEqual(rows[0]["payload"], {"buildId": "build-1", "commit": "a" * 40})

    def test_a_withdrawal_appends_no_approval_event(self) -> None:
        events = scratch_schema(self, "release_events", ledgers=(WORKSPACE_LEDGER,))
        with patch.object(event_log, "connect", events.connection):
            self.publisher.decide(
                "project", "build-1", slot="qa", decision="withdrawn", note="not yet", actor=QA
            )
        with events.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM ws_events").fetchone()["count"], 0)


UNREADABLE = "hardware/board.kicad_pcb::${KICAD10_3DMODEL_DIR}/R_0603.step"


class UnpinnedModelTests(_Desk):
    """D3: approving or publishing a build with unpinned models needs them acknowledged."""

    def setUp(self) -> None:
        super().setUp()
        self.records.models["candidate-1"] = [
            {
                "source_path": "hardware/board.kicad_pcb",
                "reference": "${KICAD10_3DMODEL_DIR}/R_0603.step",
                "location": "unreadable",
            },
            {
                "source_path": "hardware/board.kicad_pcb",
                "reference": "${KIPRJMOD}/stale.step",
                "location": "missing",
                "advisory": True,
            },
            {
                "source_path": "hardware/board.kicad_pcb",
                "reference": "/opt/models/C_0402.step",
                "location": "host",
                "digest": "e" * 64,
            },
        ]

    def test_build_detail_lists_only_the_unpinned_models(self) -> None:
        detail = self.publisher.detail("project", REPO, "build-1", QA)
        self.assertEqual(
            [row["key"] for row in detail["approvals"]["unpinned_models"]], [UNREADABLE]
        )
        # Listed, never a reason the build cannot publish.
        self.assertTrue(detail["approvals"]["can_publish"])

    def test_publish_without_the_acknowledgement_is_refused_naming_the_model(self) -> None:
        with self.assertRaises(DomainError) as caught:
            self.publish()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("R_0603.step", str(caught.exception.detail))
        self.assertNotIn("create_release", self.forge.calls)

    def test_an_acknowledged_publish_records_what_was_acknowledged(self) -> None:
        self.publisher.publish(
            "project", REPO, "build-1", tag="v1.0.0", notes="", actor=QA,
            acknowledged_models=[UNREADABLE],
        )
        self.assertEqual(
            self.records.get_publish_record("build-1")["acknowledged_models"], [UNREADABLE]
        )

    def test_approval_needs_the_acknowledgement_and_withdrawal_does_not(self) -> None:
        self.records.decisions.clear()
        with self.assertRaises(DomainError) as caught:
            self.publisher.decide(
                "project", "build-1", slot="designer", decision="approved", note="",
                actor=DESIGNER,
            )
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.records.decisions, [])
        self.publisher.decide(
            "project", "build-1", slot="designer", decision="approved", note="",
            actor=DESIGNER, acknowledged_models=[UNREADABLE],
        )
        self.assertEqual(self.records.decisions[-1]["acknowledged_models"], [UNREADABLE])
        self.publisher.decide(
            "project", "build-1", slot="designer", decision="withdrawn", note="no",
            actor=DESIGNER,
        )
        self.assertEqual(self.records.decisions[-1]["acknowledged_models"], [])

    def test_a_candidate_without_model_rows_reads_its_reasons(self) -> None:
        self.records.models.clear()
        self.records.candidates["candidate-1"]["non_hermetic_reasons"] = [
            "board.kicad_pcb: ${MODELS}/part.step is a 3D model outside the release "
            "closure that the build cannot read, so its bytes are not pinned",
            "catalogue step x (y) is not a hermetic KiCad type",
        ]
        detail = self.publisher.detail("project", REPO, "build-1", QA)
        self.assertEqual(
            [row["key"] for row in detail["approvals"]["unpinned_models"]],
            ["board.kicad_pcb::${MODELS}/part.step"],
        )


class NewTagTests(_Desk):
    def test_identity_must_name_tag_document_and_date(self) -> None:
        with self.assertRaises(DomainError) as caught:
            self.publisher.require_new_tag(REPO, {"tag": "v1.0.0"})
        self.assertEqual(caught.exception.status_code, 400)

    def test_a_tag_the_forge_already_has_is_refused(self) -> None:
        self.forge.tags.add("v1.0.0")
        identity = {"tag": "v1.0.0", "document_name": "DOC", "date": "2026-10-11"}
        with self.assertRaises(DomainError) as caught:
            self.publisher.require_new_tag(REPO, identity)
        self.assertEqual(caught.exception.status_code, 409)

    def test_a_ref_path_is_not_a_tag(self) -> None:
        identity = {"tag": "refs/tags/v1", "document_name": "DOC", "date": "2026-10-11"}
        with self.assertRaises(DomainError) as caught:
            self.publisher.require_new_tag(REPO, identity)
        self.assertEqual(caught.exception.status_code, 400)


class _GatedForge(FakeForge):
    """A forge whose every call waits until the test releases it, like a slow forge."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()
        self.entered = threading.Event()

    def _wait(self) -> None:
        self.entered.set()
        if not self.gate.wait(timeout=5):
            raise AssertionError("forge gate was never released")

    def releases(self, *, limit: int = 10):
        self._wait()
        return super().releases(limit=limit)

    def tag_exists(self, tag: str) -> bool:
        self._wait()
        return super().tag_exists(tag)

    def create_release(self, **kwargs):
        self._wait()
        return super().create_release(**kwargs)


class ForgeCallsDoNotBlockTheEventLoopTests(_Desk):
    """Build detail is polled every 3 s while Publish is open; a slow forge must not stall Prism."""

    def _assert_loop_free(self, make_call) -> None:
        forge = _GatedForge()
        publisher = ReleasePublisher(
            records=self.records,
            forges=forge,
            configuration=lambda _project_id, _candidate: dict(self.configuration),
        )

        async def scenario() -> bool:
            pending = asyncio.create_task(make_call())
            await asyncio.get_running_loop().run_in_executor(None, forge.entered.wait, 2)
            probe = asyncio.create_task(asyncio.sleep(0.01))
            done, _ = await asyncio.wait({probe}, timeout=1.0)
            free = probe in done and not pending.done()
            forge.gate.set()
            await pending
            return free

        with (
            patch.object(api, "authorized_project", return_value=REPO),
            patch.object(api, "get_project_for_role_or_404"),
            patch.object(api, "PUBLISHER", publisher),
        ):
            self.assertTrue(asyncio.run(scenario()), "a forge call blocked the event loop")

    def test_build_detail_reads_the_forge_off_the_loop(self) -> None:
        self._assert_loop_free(lambda: api.get_build("project", "build-1", QA))

    def test_tag_check_reads_the_forge_off_the_loop(self) -> None:
        self._assert_loop_free(lambda: api.check_release_tag("project", "v1.0.0", QA))

    def test_publish_calls_the_forge_off_the_loop(self) -> None:
        request = api.PublishRequest(tag="v1.0.0")
        self._assert_loop_free(lambda: api.publish_build("project", "build-1", request, QA))

    def test_build_enqueue_checks_the_tag_off_the_loop(self) -> None:
        request = api.CandidateRequest(
            commit_sha="a" * 40,
            identity={"tag": "v2.0.0", "document_name": "DOC", "date": "2026-10-11"},
        )
        with (
            patch.object(job_queue, "enqueue", return_value={"job_id": "job-1"}),
            patch.object(api.store, "save_source_defaults"),
        ):
            self._assert_loop_free(lambda: api.create_candidate("project", request, DESIGNER))


class ConfigurationSnapshotTests(unittest.TestCase):
    def test_the_default_configuration_reader_uses_the_candidate_snapshot(self) -> None:
        document = {"schema": "prism.release-studio.configuration/1", "revision": "v3"}
        candidate = {
            "configuration_snapshot_captured": True,
            "configuration_document": document,
            "technical_config_digest": technical_config_digest(document),
        }
        self.assertEqual(ReleasePublisher().configuration("project", candidate), document)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
