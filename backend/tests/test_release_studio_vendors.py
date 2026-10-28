"""Vendor packs, pipeline progress, and configuration vendors key."""

from __future__ import annotations

import hashlib
import io
import json
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tests.kicad_tool_fakes import FakeTool
from app.release_studio.canonical import write_deterministic_archive, write_deterministic_zip
from app.release_studio.config.loader import parse_configuration_yaml
from app.release_studio.config.digests import technical_config_digest
from app.release_studio.pipeline import PipelineTracker, pipeline_skeleton
from app.release_studio.vendors import generate_vendor_outputs
from app.release_studio.vendors.cli import (
    BUNDLE_NOT_READY_MESSAGE,
    reset_vendor_list_cache,
)
from app.release_studio.vendors.jlcpcb import (
    vendor_config_document,
)
from app.release_studio.vendors.pack import VendorPackError, build_vendor_pack
from app.release_studio.vendors.registry import public_profile_payload
from app.services.design_bundle import schemas
from app.services.kicad_tools import ToolResult


_MIN_CONFIG = """
schema: prism.release-studio.configuration/1
title: Demo
board: board.kicad_pcb
schematic: board.kicad_sch
jobset: Outputs.kicad_jobset
"""


class VendorRegistryTests(unittest.TestCase):
    def test_registry_lists_jlcpcb(self) -> None:
        ids = [item["id"] for item in public_profile_payload()]
        self.assertEqual(ids, ["jlcpcb"])
        self.assertTrue(all("pack_filename" in item for item in public_profile_payload()))

    def test_omitted_vendors_defaults_to_jlcpcb(self) -> None:
        parsed = parse_configuration_yaml(_MIN_CONFIG)
        self.assertEqual(parsed["vendors"], ["jlcpcb"])

    def test_unknown_vendor_is_a_load_error(self) -> None:
        from app.release_studio.config.errors import ConfigSchemaError

        with self.assertRaisesRegex(ConfigSchemaError, "unknown vendor"):
            parse_configuration_yaml(_MIN_CONFIG + "\nvendors:\n  - pcbway\n")

    def test_vendors_move_the_technical_digest(self) -> None:
        base = parse_configuration_yaml(_MIN_CONFIG)
        empty = parse_configuration_yaml(_MIN_CONFIG + "\nvendors: []\n")
        self.assertNotEqual(technical_config_digest(base), technical_config_digest(empty))


class PipelineTrackerTests(unittest.TestCase):
    def test_skeleton_has_stable_job_ids(self) -> None:
        jobs = pipeline_skeleton(vendor_ids=["jlcpcb"])["jobs"]
        self.assertEqual(
            [job["id"] for job in jobs],
            ["closure", "checks", "assembly", "artwork", "documents", "package"],
        )
        assembly = next(job for job in jobs if job["id"] == "assembly")
        self.assertIn("vendor-jlcpcb", [step["id"] for step in assembly["steps"]])

    def test_progress_payload_is_the_full_tree(self) -> None:
        emitted: list[dict] = []

        def progress(**kwargs):
            emitted.append(kwargs)

        tracker = PipelineTracker(progress, vendor_ids=["jlcpcb"])
        tracker.seed()
        tracker.start("drc")
        tracker.succeed("drc", elapsed_ms=12, log="ok")
        last = emitted[-1]
        self.assertIn("pipeline", last["payload_updates"])
        self.assertTrue(last["force"])
        self.assertEqual(emitted[1]["message"], "Running DRC")
        self.assertEqual(emitted[-1]["message"], "Finished DRC")
        checks = next(
            job
            for job in last["payload_updates"]["pipeline"]["jobs"]
            if job["id"] == "checks"
        )
        drc = next(step for step in checks["steps"] if step["id"] == "drc")
        self.assertEqual(drc["status"], "success")
        self.assertEqual(drc["elapsed_ms"], 12)


class JobPipelineStatusTests(unittest.TestCase):
    def test_slim_status_exposes_pipeline_from_payload(self) -> None:
        from app.api.jobs import _slim_status

        status = _slim_status(
            {
                "job_id": "job-1",
                "kind": "release_studio_build",
                "status": "running",
                "payload": {"pipeline": {"jobs": [{"id": "checks", "status": "in_progress"}]}},
                "result_metadata": {},
            }
        )
        self.assertEqual(status["pipeline"]["jobs"][0]["id"], "checks")

    def test_slim_status_falls_back_to_result_metadata(self) -> None:
        from app.api.jobs import _slim_status

        status = _slim_status(
            {
                "job_id": "job-1",
                "kind": "release_studio_build",
                "status": "completed",
                "payload": {},
                "result_metadata": {"pipeline": {"jobs": [{"id": "package", "status": "success"}]}},
            }
        )
        self.assertEqual(status["pipeline"]["jobs"][0]["id"], "package")


class DeterministicZipTests(unittest.TestCase):
    def test_member_order_does_not_change_bytes(self) -> None:
        first = write_deterministic_zip({"b.txt": b"b", "a.txt": b"a"})
        second = write_deterministic_zip({"a.txt": b"a", "b.txt": b"b"})
        self.assertEqual(first, second)
        self.assertEqual(first[:2], b"PK")

    def test_mtime_is_written_into_zip_headers(self) -> None:
        from datetime import datetime, timezone

        stamp = int(datetime(2026, 8, 14, 3, 39, tzinfo=timezone.utc).timestamp())
        first = write_deterministic_zip({"a.txt": b"a"}, mtime=stamp)
        second = write_deterministic_zip({"a.txt": b"a"}, mtime=stamp)
        self.assertEqual(first, second)
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            self.assertEqual(archive.getinfo("a.txt").date_time, (2026, 8, 14, 3, 39, 0))


class VendorPackTests(unittest.TestCase):
    def test_pack_contains_gerbers_and_vendor_workbooks(self) -> None:
        dossier = write_deterministic_archive(
            {
                "fabrication/gerbers/board-F_Cu.gbr": b"G04 gerber*\n",
                "fabrication/drill/board-PTH.drl": b"M48\n",
                "manufacturing/vendors/jlcpcb/bom.csv": b"Comment,Designator\n",
                "manufacturing/vendors/jlcpcb/cpl.csv": b"Designator,PosX\n",
                "manifest.json": b"{}",
            }
        )
        evidence = write_deterministic_archive(
            {
                "raw/vendors/jlcpcb/bom.xlsx": b"xlsx-bom",
                "raw/vendors/jlcpcb/cpl.xlsx": b"xlsx-cpl",
            }
        )
        pack = build_vendor_pack(
            "jlcpcb", dossier_bytes=dossier, evidence_bytes=evidence
        )
        with zipfile.ZipFile(io.BytesIO(pack)) as archive:
            names = set(archive.namelist())
        self.assertIn("gerbers/board-F_Cu.gbr", names)
        self.assertIn("drill/board-PTH.drl", names)
        self.assertIn("bom.xlsx", names)
        self.assertIn("cpl.xlsx", names)

    def test_unknown_profile_is_not_jlc_shaped(self) -> None:
        with self.assertRaisesRegex(VendorPackError, "unknown vendor"):
            build_vendor_pack("pcbway", dossier_bytes=b"not-a-tar")

    def test_second_profile_pack_is_not_jlc_shaped(self) -> None:
        from unittest.mock import patch

        class AcmeProfile:
            id = "acme"
            title = "Acme Fab"
            pack_filename = "acme-upload.zip"
            description = "fixture"

        dossier = write_deterministic_archive(
            {
                "fabrication/gerbers/board-F_Cu.gbr": b"G04 gerber*\n",
                "fabrication/drill/board-PTH.drl": b"M48\n",
                "manufacturing/vendors/acme/paste.csv": b"ref,x\n",
                "manufacturing/vendors/jlcpcb/bom.csv": b"Comment,Designator\n",
            }
        )
        evidence = write_deterministic_archive(
            {
                "raw/vendors/acme/notes.txt": b"acme-notes",
                "raw/vendors/jlcpcb/bom.xlsx": b"xlsx-bom",
            }
        )
        with patch(
            "app.release_studio.vendors.pack.profile_by_id",
            return_value=AcmeProfile(),
        ):
            pack = build_vendor_pack(
                "acme", dossier_bytes=dossier, evidence_bytes=evidence
            )
        with zipfile.ZipFile(io.BytesIO(pack)) as archive:
            names = set(archive.namelist())
        self.assertIn("gerbers/board-F_Cu.gbr", names)
        self.assertIn("paste.csv", names)
        self.assertIn("notes.txt", names)
        self.assertNotIn("bom.csv", names)
        self.assertNotIn("bom.xlsx", names)

    def test_missing_gerbers_fail_closed(self) -> None:
        dossier = write_deterministic_archive({"manifest.json": b"{}"})
        with self.assertRaisesRegex(VendorPackError, "missing gerber"):
            build_vendor_pack("jlcpcb", dossier_bytes=dossier)

    def test_incomplete_jlc_pack_is_not_ready_and_names_every_missing_requirement(self) -> None:
        from app.release_studio.vendors.pack import vendor_pack_readiness

        dossier = write_deterministic_archive(
            {"fabrication/gerbers/board-F_Cu.gbr": b"G04 gerber*\n"}
        )
        readiness = vendor_pack_readiness("jlcpcb", dossier_bytes=dossier)
        self.assertFalse(readiness["ready"])
        self.assertEqual(
            readiness["missing_requirements"],
            ["drill", "bom.csv", "cpl.csv", "bom.xlsx", "cpl.xlsx"],
        )
        from fastapi.encoders import jsonable_encoder

        from app.api import release_studio_models as models

        self.assertEqual(
            models.VendorReadiness.model_validate(readiness).model_dump(mode="json"),
            jsonable_encoder(readiness),
        )
        with self.assertRaisesRegex(VendorPackError, "missing drill, bom.csv, cpl.csv, bom.xlsx, cpl.xlsx"):
            build_vendor_pack("jlcpcb", dossier_bytes=dossier)


class JlcpcbGeneratorTests(unittest.TestCase):
    def test_the_vendor_config_pins_what_reaches_a_fab_house(self) -> None:
        """Two config defaults would change the BOM, so they are asserted here.

        The template ships `include_dnp: true`, which puts do-not-populate
        parts in the assembler's BOM, and groups on description, which split
        one OBC line into three. Both were found by diffing real output.
        """

        import json
        import re

        from app.release_studio.vendors.jlcpcb import VENDOR_CONFIG

        text = VENDOR_CONFIG.read_text(encoding="utf-8")
        body = re.sub(r"^\s*//.*$", "", text, flags=re.M)
        config = json.loads(body)
        self.assertIs(config["bom"]["include_dnp"], False)
        self.assertEqual(config["bom"]["group_fields"], ["value", "footprint"])
        self.assertEqual(config["variants"]["mode"], "base")

    def test_editing_the_vendor_config_moves_the_build_key(self) -> None:
        """Otherwise a pack could change under an unchanged build key."""

        from unittest.mock import patch

        from app.release_studio.documents import renderer_resource_digest

        before = renderer_resource_digest()
        with patch(
            "app.release_studio.vendors.jlcpcb.VENDOR_CONFIG"
        ) as fake:
            fake.read_bytes.return_value = b'{"schema": "changed"}'
            after = renderer_resource_digest()
        self.assertNotEqual(before, after)


def _vendor_list_document() -> dict:
    return {
        "schema": "prism.cli.vendor_list.a0",
        "vendors": [
            {
                "id": "jlcpcb",
                "name": "JLCPCB",
                "outputs": [
                    {"kind": "bom", "format": "csv", "version": "1.0.0"},
                    {"kind": "bom", "format": "xlsx", "version": "1.0.0"},
                    {"kind": "cpl", "format": "csv", "version": "1.0.0"},
                    {"kind": "cpl", "format": "xlsx", "version": "1.0.0"},
                ],
                "config_schema": "prism.vendor.jlcpcb.config.a0",
            }
        ],
    }


def _error_document(*, code: str, message: str, category: str = "usage") -> dict:
    return {
        "schema": "prism.cli.error.a0",
        "category": category,
        "code": code,
        "message": message,
    }


def _run_result_document(files: dict[str, bytes]) -> dict:
    return {
        "schema": "prism.cli.vendor_run_result.a0",
        "vendor": "jlcpcb",
        "files": [
            {
                "path": name,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for name, payload in sorted(files.items())
        ],
        "rows": {"bom": 2, "cpl": 2},
        "excluded": [
            {
                "key": "board.kicad_pcb#0fe3d08e-86fc-4dd5-bd2f-bcbc5d801a5d",
                "reference": "R6",
                "code": "excluded-from-bom",
                "message": "Do-not-populate component excluded from the BOM.",
            }
        ],
    }


class BundleVendorPathTests(unittest.TestCase):
    """``prism-kicad vendor run`` is the pack source when the bundle is ready."""

    def setUp(self) -> None:
        reset_vendor_list_cache()
        self._counter = 0

    def setUpTmp(self) -> Path:
        import tempfile

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def _cli(self, run_handler) -> FakeTool:
        self._counter += 1

        def handler(argv, **kwargs):  # noqa: ARG001
            if list(argv[:2]) == ["vendor", "list"]:
                return ToolResult(0, json.dumps(_vendor_list_document()))
            return run_handler(argv)

        # A unique name keeps the per-generator vendor-list cache from leaking
        # between tests; a real worker keys it by the binary's path and mtime.
        return FakeTool(handler, name=f"prism-kicad-f2-{self._counter}", version="test")

    def _files(self) -> dict[str, bytes]:
        return {
            "bom.csv": b"Comment,Designator\nR,R1\n",
            "cpl.csv": b"Designator,Mid X\nR1,1\n",
            "bom.xlsx": b"xlsx-bom",
            "cpl.xlsx": b"xlsx-cpl",
        }

    def _run_handler(self, *, validate_config: bool = False):
        files = self._files()

        def handler(argv):
            output = Path(argv[argv.index("-o") + 1])
            output.mkdir(parents=True, exist_ok=True)
            if validate_config:
                config = Path(argv[argv.index("--config") + 1])
                document = json.loads(config.read_text(encoding="utf-8"))
                schemas.validate(document, "prism.vendor.jlcpcb.config.a0")
                self.assertEqual(document["bom"]["group_fields"], ["value", "footprint"])
            for name, payload in files.items():
                (output / name).write_bytes(payload)
            return ToolResult(0, json.dumps(_run_result_document(files)))

        return handler

    def test_the_bundle_path_writes_todays_member_names(self) -> None:
        root = self.setUpTmp()
        bundle = root / "abc.prism"
        bundle.write_bytes(b"bundle")
        cli = self._cli(self._run_handler(validate_config=True))

        outputs, evidence = generate_vendor_outputs(
            config={"board": "board.kicad_pcb", "vendors": ["jlcpcb"]},
            closure_root=root,
            output_root=root / "out",
            cli=cli,
            bundle=bundle,
        )

        self.assertEqual(len(outputs), 1)
        output = outputs[0]
        self.assertEqual(output.step_id, "vendor-jlcpcb")
        self.assertEqual(output.returncode, 0)
        self.assertEqual(
            sorted(path.relative_to(root / "out").as_posix() for path in output.files),
            ["manufacturing/vendors/jlcpcb/bom.csv", "manufacturing/vendors/jlcpcb/cpl.csv"],
        )
        self.assertEqual(
            sorted(evidence),
            ["vendors/jlcpcb/bom.xlsx", "vendors/jlcpcb/cpl.xlsx"],
        )
        # The excluded component is surfaced like Cruncher's warnings were.
        self.assertIn("excluded R6", output.stdout)
        self.assertIn("excluded-from-bom", output.stdout)
        # The recorded argv names the bundle, never a host directory.
        self.assertEqual(len(output.normalized_argv), 1)
        self.assertTrue(output.normalized_argv[0].startswith("prism-kicad vendor run jlcpcb"))
        self.assertNotIn(str(root), output.normalized_argv[0])
        # The config was written from Prism's one pinned file and validated.
        self.assertTrue((root / "out" / "_vendor_work" / "jlcpcb" / "jlcpcb.config.json").is_file())

    def test_a_sha256_mismatch_fails_the_step(self) -> None:
        root = self.setUpTmp()
        bundle = root / "abc.prism"
        bundle.write_bytes(b"bundle")

        def handler(argv):
            output = Path(argv[argv.index("-o") + 1])
            output.mkdir(parents=True, exist_ok=True)
            files = self._files()
            for name, payload in files.items():
                (output / name).write_bytes(payload)
            # The document names different bytes than were written.
            document = _run_result_document(
                {name: payload + b"tampered" for name, payload in files.items()}
            )
            return ToolResult(0, json.dumps(document))

        outputs, _ = generate_vendor_outputs(
            config={"board": "board.kicad_pcb", "vendors": ["jlcpcb"]},
            closure_root=root,
            output_root=root / "out",
            cli=self._cli(handler),
            bundle=bundle,
        )
        self.assertEqual(outputs[0].returncode, 1)
        self.assertIn("sha256", outputs[0].stderr)
        self.assertFalse(outputs[0].skipped_reason)

    def test_vendor_unknown_is_a_configuration_error_naming_the_id(self) -> None:
        root = self.setUpTmp()
        bundle = root / "abc.prism"
        bundle.write_bytes(b"bundle")

        def handler(argv):
            return ToolResult(
                2,
                json.dumps(
                    _error_document(
                        code="vendor-unknown",
                        message='no vendor is registered as "jlcpcb"',
                    )
                ),
            )

        outputs, _ = generate_vendor_outputs(
            config={"board": "board.kicad_pcb", "vendors": ["jlcpcb"]},
            closure_root=root,
            output_root=root / "out",
            cli=self._cli(handler),
            bundle=bundle,
        )
        self.assertEqual(outputs[0].returncode, 1)
        self.assertIn("configuration error", outputs[0].stderr)
        self.assertIn("jlcpcb", outputs[0].stderr)

    def test_a_precondition_exit_waits_for_parse(self) -> None:
        root = self.setUpTmp()
        bundle = root / "abc.prism"
        bundle.write_bytes(b"bundle")

        def handler(argv):
            return ToolResult(
                7,
                json.dumps(
                    _error_document(
                        code="member-not-found",
                        message="the bundle has no projections/placements.json",
                        category="precondition",
                    )
                ),
            )

        outputs, _ = generate_vendor_outputs(
            config={"board": "board.kicad_pcb", "vendors": ["jlcpcb"]},
            closure_root=root,
            output_root=root / "out",
            cli=self._cli(handler),
            bundle=bundle,
        )
        self.assertEqual(outputs[0].returncode, 1)
        self.assertEqual(outputs[0].stderr, BUNDLE_NOT_READY_MESSAGE)

    def test_another_failure_uses_the_error_documents_message(self) -> None:
        root = self.setUpTmp()
        bundle = root / "abc.prism"
        bundle.write_bytes(b"bundle")

        def handler(argv):
            return ToolResult(
                3,
                json.dumps(
                    _error_document(
                        code="input-invalid",
                        message="--config is not a JSON document",
                        category="input",
                    )
                ),
            )

        outputs, _ = generate_vendor_outputs(
            config={"board": "board.kicad_pcb", "vendors": ["jlcpcb"]},
            closure_root=root,
            output_root=root / "out",
            cli=self._cli(handler),
            bundle=bundle,
        )
        self.assertEqual(outputs[0].returncode, 1)
        self.assertEqual(outputs[0].stderr, "--config is not a JSON document")

    def test_a_missing_bundle_fails_the_step(self) -> None:
        root = self.setUpTmp()
        outputs, evidence = generate_vendor_outputs(
            config={"board": "board.kicad_pcb", "vendors": ["jlcpcb"]},
            closure_root=root,
            output_root=root / "out",
            cli=None,
            bundle=None,
        )
        self.assertEqual(outputs[0].returncode, 1)
        self.assertIn("design bundle", outputs[0].stderr)
        self.assertEqual(evidence, {})


class VendorConfigConversionTests(unittest.TestCase):
    def test_the_python_conversion_is_the_checked_in_document(self) -> None:
        document = vendor_config_document()
        self.assertEqual(document["schema"], "prism.vendor.jlcpcb.config.a0")
        self.assertEqual(
            document["bom"]["outputs"], ["jlc-csv", "jlc-xlsx"]
        )
        self.assertEqual(
            document["bom"]["group_fields"], ["value", "footprint"]
        )
        self.assertIs(document["bom"]["include_dnp"], False)
        self.assertIs(document["bom"]["split_dnp"], False)
        self.assertEqual(document["variants"]["mode"], "base")
        self.assertEqual(document["pnp"]["units"], "mm")
        # Validated against the generated schema, not a second hand-written file.
        schemas.validate(document, "prism.vendor.jlcpcb.config.a0")

    def test_a_value_outside_the_schema_is_rejected(self) -> None:
        root = Path(self._tmp())
        config = root / "jlcpcb.config.jsonc"
        config.write_text(
            '{"schema":"kicad_cruncher.bom.config.a0",'
            '"variants":{"mode":"sometimes","names":[],"include_base":true},'
            '"bom":{"outputs":["jlc-csv"],"group_fields":["value"],'
            '"include_dnp":false,"split_dnp":false},'
            '"pnp":{"outputs":["jlc-cpl"],"units":"mm"}}',
            encoding="utf-8",
        )
        with self.assertRaises(schemas.SchemaInvalid):
            vendor_config_document(config)

    def _tmp(self) -> str:
        import tempfile

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return directory.name


if __name__ == "__main__":
    unittest.main()
