"""Per-release inputs: IPC options, impedance CSV, synthesized config, source discovery."""

from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.release_studio.impedance import TEMPLATE_CSV, parse_impedance_csv
from app.services.job_service import jobs as job_queue
from app.release_studio.inputs import synthesize_configuration
from app.release_studio.ipc import OTHER, public_ipc_payload
from app.release_studio.source import apply_source_defaults, discover_source
from app.services.design_bundle import consumer
from app.services.design_bundle.errors import DesignDataPending
from app.services.design_bundle.manifest import Readiness
from app.services.kicad_tools import isolated_env
from support.repo import init_repo
from tests.kicad_tool_fakes import FakeTool


class IpcOptionTests(unittest.TestCase):
    def test_public_payload_includes_classes_and_other(self) -> None:
        payload = public_ipc_payload()
        manufacturing = [item["value"] for item in payload["manufacturing"]]
        assembly = [item["value"] for item in payload["assembly"]]
        self.assertIn("IPC-6012 Class 2", manufacturing)
        self.assertIn("IPC-6013 Class 3", manufacturing)
        self.assertIn(OTHER, manufacturing)
        self.assertIn("IPC-A-610 Class 2", assembly)
        self.assertIn("J-STD-001 Class 3", assembly)
        self.assertIn(OTHER, assembly)

    def test_board_characteristic_dropdowns_include_other(self) -> None:
        payload = public_ipc_payload()
        self.assertIn("Green", [item["value"] for item in payload["solder_mask_colour"]])
        self.assertIn("White", [item["value"] for item in payload["silkscreen_colour"]])
        self.assertIn("Tented", [item["value"] for item in payload["via_treatment"]])
        self.assertIn(OTHER, [item["value"] for item in payload["solder_mask_colour"]])
        self.assertIn(OTHER, [item["value"] for item in payload["silkscreen_colour"]])
        self.assertIn(OTHER, [item["value"] for item in payload["via_treatment"]])


class IsolatedCliEnvTests(unittest.TestCase):
    def test_points_temp_and_runtime_at_the_scratch_dir(self) -> None:
        env = isolated_env(
            Path("/tmp/kicad-cli-test"),
            base={"PATH": "/usr/bin", "TMPDIR": "/tmp"},
        )
        self.assertEqual(env["TMPDIR"], "/tmp/kicad-cli-test")
        self.assertEqual(env["TMP"], "/tmp/kicad-cli-test")
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/tmp/kicad-cli-test")
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertNotIn("KICAD_CONFIG_HOME", env)

    def test_default_variant_is_omitted_from_bom_and_positions(self) -> None:
        captured: list[list[str]] = []

        def run(argv, **_kwargs):
            captured.append([str(part) for part in argv])
            out = Path(argv[argv.index("--output") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("ok\n")
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "board.kicad_pcb").write_text("(kicad_pcb)\n")
            (root / "board.kicad_sch").write_text("(kicad_sch)\n")
            from app.release_studio.steps import run_step_catalogue

            run_step_catalogue(
                closure_root=root,
                board_rel="board.kicad_pcb",
                schematic_rel="board.kicad_sch",
                output_root=root / "out",
                tool=FakeTool(run),
                variant="default",
                bom_preset="Grouped By Value",
                only=("bom", "positions"),
            )
        self.assertEqual(len(captured), 2)
        for argv in captured:
            self.assertNotIn("--variant", argv)
        bom = next(argv for argv in captured if "bom" in argv)
        self.assertNotIn("--preset", bom)
        self.assertIn("--group-by", bom)
        self.assertIn("Value,DNP", bom)
        fields = bom[bom.index("--fields") + 1]
        self.assertIn("Manufacturer", fields)
        self.assertTrue(fields.startswith("Reference,QUANTITY,"))

    def test_custom_bom_preset_still_uses_the_named_preset_flag(self) -> None:
        captured: list[list[str]] = []

        def run(argv, **_kwargs):
            captured.append([str(part) for part in argv])
            out = Path(argv[argv.index("--output") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("ok\n")
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "board.kicad_sch").write_text("(kicad_sch)\n")
            from app.release_studio.steps import run_step_catalogue

            run_step_catalogue(
                closure_root=root,
                board_rel="",
                schematic_rel="board.kicad_sch",
                output_root=root / "out",
                tool=FakeTool(run),
                bom_preset="My House BOM",
                only=("bom",),
            )
        bom = captured[0]
        self.assertIn("--preset", bom)
        self.assertIn("My House BOM", bom)
        self.assertNotIn("--group-by", bom)


class ImpedanceCsvTests(unittest.TestCase):
    def test_template_has_the_locked_columns(self) -> None:
        header = TEMPLATE_CSV.splitlines()[0]
        self.assertEqual(
            header,
            "Type,Name,Layer pair,Target Z (Ω),Tolerance (Ω),Width (mm),Spacing (mm),Notes",
        )

    def test_parse_skips_blank_rows(self) -> None:
        rows = parse_impedance_csv(TEMPLATE_CSV + "\n,,,,,,,\n")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["Type"], "SE")
        self.assertEqual(rows[1]["Name"], "USB")


class SynthesizeConfigurationTests(unittest.TestCase):
    def test_identity_and_manufacturing_become_document_fields(self) -> None:
        document = synthesize_configuration(
            board="board.kicad_pcb",
            schematic="board.kicad_sch",
            variant="A",
            document_name="USBPD-100",
            tag="v1.0.0",
            date="2026-08-14",
            notes="pilot",
            manufacturing={
                "manufacturing_ipc_class": "IPC-6012 Class 2",
                "assembly_ipc_class": "IPC-A-610 Class 2",
                "solder_mask_colour": "Green",
                "silkscreen_colour": "White",
                "via_treatment": "Tented",
                "vendors": ["jlcpcb"],
            },
            bom_preset="Grouped By Value",
        )
        self.assertEqual(document["document_number"], "USBPD-100")
        self.assertEqual(document["revision"], "v1.0.0")
        self.assertEqual(document["release_date"], "2026-08-14")
        self.assertEqual(document["release_notes"], "pilot")
        self.assertEqual(document["bom_preset"], "Grouped By Value")
        self.assertEqual(document["fields"]["silkscreen_colour"], "White")
        self.assertEqual(document["fields"]["manufacturing_ipc_class"], "IPC-6012 Class 2")
        self.assertEqual(document["vendors"], ["jlcpcb"])
        self.assertNotIn("jobset", document)


class SourceDiscoveryTests(unittest.TestCase):
    def test_discovers_board_schematic_and_bom_presets(self) -> None:
        files = [
            "board.kicad_pcb",
            "board.kicad_sch",
            "Outputs.kicad_jobset",
            "board.kicad_pro",
        ]
        with (
            patch("app.release_studio.source._ls_tree", return_value=files),
            patch("app.release_studio.source._bom_presets", return_value=["Custom BOM"]),
            patch("app.release_studio.source._variants", return_value=["A"]),
        ):
            source = discover_source(Path("/unused"), "a" * 40)
        self.assertEqual(source["board"], "board.kicad_pcb")
        self.assertEqual(source["schematic"], "board.kicad_sch")
        self.assertNotIn("jobset", source)
        self.assertNotIn("jobsets", source)
        self.assertIn("Custom BOM", source["bom_presets"])
        self.assertIn("Current project settings", source["bom_presets"])
        # The explicit default is offered before the discovered names.
        self.assertEqual(source["variants"], ["default", "A"])
        self.assertEqual(source["variant"], "default")

    def test_variants_come_from_the_shared_catalog_for_the_scoped_source(self) -> None:
        from app.release_studio import source as source_module

        payload = {
            "variants": [
                {"name": "default", "description": None, "sources": ["project"]},
                {"name": "Lite", "description": None, "sources": ["schematic"]},
                {"name": "Lite", "description": None, "sources": ["pcb"]},
                {"name": "  ", "description": None, "sources": []},
                {"name": "Pro", "description": None, "sources": ["pcb"]},
            ]
        }
        with tempfile.TemporaryDirectory() as raw:
            repo_root = Path(raw)
            scoped = repo_root / "hardware" / "board"
            scoped.mkdir(parents=True)
            (scoped / "board.kicad_pro").write_text("{}\n")
            with patch(
                "app.services.variant_catalog_service.discover_variant_catalog",
                return_value=payload,
            ) as discover:
                names = source_module._variants(
                    repo_root,
                    "b" * 40,
                    "hardware/board",
                    "hardware/board/board.kicad_pro",
                    "prj_board",
                )
        self.assertEqual(names, ["Lite", "Pro"])
        project, commit = discover.call_args.args
        self.assertEqual(commit, "b" * 40)
        self.assertEqual(project.path, str(scoped.resolve()))
        self.assertEqual(project.project_file, "board.kicad_pro")
        self.assertEqual(project.id, "prj_board")

    def test_source_discovery_hands_the_project_id_to_the_catalog(self) -> None:
        # The catalog reads the design bundle by project id; an empty id made
        # every Release Studio lookup miss the bundle with a traceback.
        files = ["board.kicad_pcb", "board.kicad_sch", "board.kicad_pro"]
        with (
            patch("app.release_studio.source._ls_tree", return_value=files),
            patch("app.release_studio.source._bom_presets", return_value=[]),
            patch(
                "app.services.variant_catalog_service.discover_variant_catalog",
                return_value={"variants": [{"name": "Lite"}]},
            ) as discover,
        ):
            source = discover_source(Path("/unused"), "a" * 40, project_id="prj_1")
        self.assertEqual(source["variants"], ["default", "Lite"])
        project, _commit = discover.call_args.args
        self.assertEqual(project.id, "prj_1")

    def test_variant_discovery_failure_offers_only_the_default(self) -> None:
        from app.release_studio import source as source_module

        files = ["board.kicad_pcb", "board.kicad_sch", "board.kicad_pro"]
        with (
            patch("app.release_studio.source._ls_tree", return_value=files),
            patch("app.release_studio.source._bom_presets", return_value=[]),
            patch(
                "app.services.variant_catalog_service.discover_variant_catalog",
                side_effect=ValueError("no project file in this commit"),
            ),
        ):
            source = discover_source(
                Path("/unused"), "a" * 40, "hardware/board", project_id="prj_1"
            )
        self.assertEqual(source["variants"], ["default"])
        self.assertEqual(source["variant"], "default")

    def test_no_project_file_skips_catalog_discovery(self) -> None:
        from app.release_studio import source as source_module

        with patch(
            "app.services.variant_catalog_service.discover_variant_catalog"
        ) as discover:
            names = source_module._variants(Path("/repo"), "b" * 40, None, None)
        self.assertEqual(names, [])
        discover.assert_not_called()

    def test_without_a_project_id_the_catalog_is_not_read(self) -> None:
        # The bundle is keyed by project id; a checkout path alone cannot name one.
        from app.release_studio import source as source_module

        with patch(
            "app.services.variant_catalog_service.discover_variant_catalog"
        ) as discover:
            names = source_module._variants(
                Path("/repo"), "b" * 40, "hardware/board", "hardware/board/board.kicad_pro"
            )
        self.assertEqual(names, [])
        discover.assert_not_called()

    def test_a_pending_bundle_is_logged_distinctly_and_offers_the_default(self) -> None:
        from app.release_studio import source as source_module

        with patch(
            "app.services.variant_catalog_service.discover_variant_catalog",
            side_effect=DesignDataPending(["projections/semantic-index.json"]),
        ):
            with self.assertLogs("app.release_studio.source", level="INFO") as logs:
                names = source_module._variants(
                    Path("/repo"), "b" * 40, None, "board.kicad_pro", "prj_1"
                )
        self.assertEqual(names, [])
        self.assertIn("still building", "\n".join(logs.output))
        self.assertNotIn("Traceback", "\n".join(logs.output))

    def test_the_release_catalog_project_reads_the_bundle_by_id(self) -> None:
        # Release Studio's _CatalogProject has no workspace row: the bundle
        # read keys on its id and resolves the commit from its checkout.
        from app.release_studio import source as source_module

        class _Opened:
            absent = False
            working_tree_dirty = False

            def readiness(self, paths):
                return {path: Readiness(path, "ready", "parse", None) for path in paths}

            def read_json(self, path):
                return {
                    "assembly": {
                        "catalog": [
                            {"name": "Lite", "description": None, "sources": ["project"]}
                        ],
                        "diagnostics": [],
                    }
                }

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            sha = init_repo(
                root,
                {
                    "hardware/board/board.kicad_pro": "{}\n",
                    "hardware/board/board.kicad_pcb": "(kicad_pcb)\n",
                },
            )
            with patch.object(
                consumer.reader, "open_revision", return_value=_Opened()
            ) as open_revision:
                names = source_module._variants(
                    root,
                    sha,
                    "hardware/board",
                    "hardware/board/board.kicad_pro",
                    "prj_board",
                )
        self.assertEqual(names, ["Lite"])
        self.assertEqual(open_revision.call_args.args[0], "prj_board")
        self.assertEqual(open_revision.call_args.args[1], sha)


class ApplySourceDefaultsTests(unittest.TestCase):
    def test_prefers_saved_picks_that_still_exist(self) -> None:
        discovered = {
            "boards": ["a.kicad_pcb", "b.kicad_pcb"],
            "schematics": ["a.kicad_sch", "b.kicad_sch"],
            "board": "a.kicad_pcb",
            "schematic": "a.kicad_sch",
            "variants": ["default", "assembly"],
            "bom_presets": ["Current project settings", "Grouped By Value"],
            "default_bom_preset": "Current project settings",
            "variant": "",
        }
        result = apply_source_defaults(
            discovered,
            {
                "board": "b.kicad_pcb",
                "schematic": "b.kicad_sch",
                "variant": "assembly",
                "bom_preset": "Grouped By Value",
            },
        )
        self.assertEqual(result["board"], "b.kicad_pcb")
        self.assertEqual(result["schematic"], "b.kicad_sch")
        self.assertEqual(result["variant"], "assembly")
        self.assertEqual(result["default_bom_preset"], "Grouped By Value")

    def test_ignores_saved_picks_missing_from_the_commit(self) -> None:
        discovered = {
            "boards": ["a.kicad_pcb"],
            "schematics": ["a.kicad_sch"],
            "board": "a.kicad_pcb",
            "schematic": "a.kicad_sch",
            "variants": ["default"],
            "bom_presets": ["Current project settings"],
            "default_bom_preset": "Current project settings",
            "variant": "",
        }
        result = apply_source_defaults(
            discovered,
            {
                "board": "gone.kicad_pcb",
                "schematic": "gone.kicad_sch",
                "variant": "legacy",
                "bom_preset": "Missing preset",
            },
        )
        self.assertEqual(result["board"], "a.kicad_pcb")
        self.assertEqual(result["schematic"], "a.kicad_sch")
        self.assertEqual(result["variant"], "default")
        self.assertEqual(result["default_bom_preset"], "Current project settings")

    def test_explicit_default_survives_named_variants(self) -> None:
        discovered = {
            "boards": ["a.kicad_pcb"],
            "schematics": ["a.kicad_sch"],
            "board": "a.kicad_pcb",
            "schematic": "a.kicad_sch",
            "variants": ["default", "assembly"],
            "bom_presets": [],
            "default_bom_preset": "",
            "variant": "",
        }
        result = apply_source_defaults(discovered, {"variant": "default"})
        self.assertEqual(result["variant"], "default")

    def test_removed_named_variant_falls_back_to_default_not_another_name(self) -> None:
        discovered = {
            "boards": ["a.kicad_pcb"],
            "schematics": ["a.kicad_sch"],
            "board": "a.kicad_pcb",
            "schematic": "a.kicad_sch",
            "variants": ["default", "assembly", "pro"],
            "bom_presets": [],
            "default_bom_preset": "",
            "variant": "",
        }
        result = apply_source_defaults(discovered, {"variant": "assembly-v2"})
        self.assertEqual(result["variant"], "default")


class ReleaseStudioInputApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.api import release_studio as api

        self.api = api

        class _User:
            email = "designer@example.com"
            role = "designer"

        self.user = _User()

    def test_candidate_request_accepts_identity_and_uploads(self) -> None:
        request = self.api.CandidateRequest(
            commit_sha="a" * 40,
            board="board.kicad_pcb",
            schematic="board.kicad_sch",
            identity={"tag": "v1.0.0", "document_name": "USBPD-100", "date": "2026-08-14", "notes": ""},
            manufacturing={"manufacturing_ipc_class": "IPC-6012 Class 2"},
            impedance_csv=TEMPLATE_CSV,
        )
        self.assertEqual(request.identity["tag"], "v1.0.0")
        self.assertIn("Type", request.impedance_csv)

    def test_source_endpoint_returns_ipc_options(self) -> None:
        import asyncio

        with (
            patch.object(self.api, "get_project_for_role_or_404"),
            patch.object(
                self.api,
                "authorized_project",
                return_value={"path": "/tmp/project", "repo_url": "https://github.com/org/repo.git"},
            ),
            patch("app.release_studio.source.discover_source", return_value={"board": "board.kicad_pcb"}),
            patch.object(self.api.store, "get_source_defaults", return_value={}),
            patch(
                "app.services.forge_publish_service.describe_forge",
                return_value=type("Forge", (), {"to_dict": lambda self: {"kind": "github", "name": "GitHub", "host": "github.com", "owner_repo": "org/repo", "token_configured": True, "token_hint": ""}})(),
            ),
        ):
            payload = asyncio.run(self.api.get_source("proj", commit_sha="a" * 40, user=self.user))
        self.assertEqual(payload["source"]["board"], "board.kicad_pcb")
        self.assertTrue(any(item["value"] == "other" for item in payload["ipc"]["manufacturing"]))

    def test_source_endpoint_applies_saved_defaults(self) -> None:
        import asyncio

        discovered = {
            "boards": ["a.kicad_pcb", "b.kicad_pcb"],
            "schematics": ["a.kicad_sch"],
            "board": "a.kicad_pcb",
            "schematic": "a.kicad_sch",
            "project": "a.kicad_pro",
            "variants": ["default", "assembly"],
            "bom_presets": ["Current project settings", "Grouped By Value"],
            "default_bom_preset": "Current project settings",
            "variant": "default",
        }
        with (
            patch.object(self.api, "get_project_for_role_or_404"),
            patch.object(
                self.api,
                "authorized_project",
                return_value={"path": "/tmp/project", "repo_url": "https://github.com/org/repo.git"},
            ),
            patch("app.release_studio.source.discover_source", return_value=discovered),
            patch.object(
                self.api.store,
                "get_source_defaults",
                return_value={
                    "board": "b.kicad_pcb",
                    "schematic": "",
                    "variant": "assembly",
                    "bom_preset": "Grouped By Value",
                },
            ),
            patch(
                "app.services.forge_publish_service.describe_forge",
                return_value=type("Forge", (), {"to_dict": lambda self: {"kind": "github", "name": "GitHub", "host": "github.com", "owner_repo": "org/repo", "token_configured": True, "token_hint": ""}})(),
            ),
        ):
            payload = asyncio.run(self.api.get_source("proj", commit_sha="a" * 40, user=self.user))
        self.assertEqual(payload["source"]["board"], "b.kicad_pcb")
        self.assertEqual(payload["source"]["variant"], "assembly")
        self.assertEqual(payload["source"]["default_bom_preset"], "Grouped By Value")
        from fastapi.encoders import jsonable_encoder

        from app.api import release_studio_models as models

        self.assertEqual(
            models.SourceResponse.model_validate(payload).model_dump(mode="json"),
            jsonable_encoder(payload),
        )

    def test_put_source_defaults_persists(self) -> None:
        import asyncio

        request = self.api.SourceDefaultsRequest(board="b.kicad_pcb", variant="assembly")
        saved = {
            "board": "b.kicad_pcb",
            "schematic": "",
            "variant": "assembly",
            "bom_preset": "",
        }
        with (
            patch.object(self.api, "get_project_for_role_or_404"),
            patch.object(self.api.store, "save_source_defaults", return_value=saved) as persist,
        ):
            payload = asyncio.run(self.api.save_source_defaults("proj", request, user=self.user))
        persist.assert_called_once()
        self.assertEqual(persist.call_args.args[0], "proj")
        self.assertEqual(payload["defaults"]["board"], "b.kicad_pcb")
        from fastapi.encoders import jsonable_encoder

        from app.api import release_studio_models as models

        self.assertEqual(
            models.SourceDefaultsResponse.model_validate(payload).model_dump(mode="json"),
            jsonable_encoder(payload),
        )

    def test_create_candidate_remembers_source_picks(self) -> None:
        import asyncio

        request = self.api.CandidateRequest(
            commit_sha="a" * 40,
            board="board.kicad_pcb",
            schematic="board.kicad_sch",
            variant="assembly",
            bom_preset="Grouped By Value",
            identity={"tag": "v1.0.0", "document_name": "DOC", "date": "2026-08-14"},
        )
        with (
            patch.object(self.api, "get_project_for_role_or_404"),
            patch.object(job_queue, "enqueue", return_value={"job_id": "job-1"}),
            patch.object(self.api.store, "save_source_defaults") as persist,
            patch.object(self.api, "authorized_project", return_value={"repo_url": ""}),
            patch.object(self.api.PUBLISHER, "tag_taken", return_value=False),
        ):
            payload = asyncio.run(self.api.create_candidate("proj", request, user=self.user))
        persist.assert_called_once()
        self.assertEqual(persist.call_args.args[0], "proj")
        self.assertEqual(persist.call_args.args[1]["board"], "board.kicad_pcb")
        self.assertEqual(persist.call_args.args[1]["variant"], "assembly")
        self.assertEqual(payload["job"]["job_id"], "job-1")

    def test_create_candidate_succeeds_when_defaults_cannot_be_saved(self) -> None:
        import asyncio

        request = self.api.CandidateRequest(
            commit_sha="a" * 40,
            board="board.kicad_pcb",
            identity={"tag": "v1.0.0", "document_name": "DOC", "date": "2026-08-14"},
        )
        with (
            patch.object(self.api, "get_project_for_role_or_404"),
            patch.object(job_queue, "enqueue", return_value={"job_id": "job-1"}),
            patch.object(self.api.store, "save_source_defaults", side_effect=RuntimeError("db down")),
            patch.object(self.api, "authorized_project", return_value={"repo_url": ""}),
            patch.object(self.api.PUBLISHER, "tag_taken", return_value=False),
        ):
            payload = asyncio.run(self.api.create_candidate("proj", request, user=self.user))
        self.assertEqual(payload["job"]["job_id"], "job-1")

    def test_tag_check_uses_the_forge(self) -> None:
        import asyncio

        from tests.release_publish_fakes import FakeForge

        forge = FakeForge()
        forge.tags.add("v1.0.0")

        with (
            patch.object(self.api, "get_project_for_role_or_404"),
            patch.object(
                self.api,
                "authorized_project",
                return_value={"repo_url": "https://github.com/org/repo.git"},
            ),
            patch("app.services.forge_publish_service.forge_for", return_value=forge) as forge_for,
        ):
            payload = asyncio.run(self.api.check_release_tag("proj", "v1.0.0", user=self.user))
        forge_for.assert_called_once_with("https://github.com/org/repo.git")
        self.assertEqual(forge.calls, ["tag_exists"])
        self.assertTrue(payload["exists"])
        from fastapi.encoders import jsonable_encoder

        from app.api import release_studio_models as models

        self.assertEqual(
            models.ReleaseTagCheck.model_validate(payload).model_dump(mode="json"),
            jsonable_encoder(payload),
        )

    def test_impedance_template_is_csv(self) -> None:
        import asyncio

        with patch.object(self.api, "get_project_for_role_or_404"):
            response = asyncio.run(self.api.impedance_template("proj", user=self.user))
        self.assertEqual(response.media_type, "text/csv")
        self.assertIn(b"Target Z", response.body)

    def test_source_rejects_short_commit(self) -> None:
        import asyncio

        with patch.object(self.api, "authorized_project", return_value={}):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(self.api.get_source("proj", commit_sha="abc", user=self.user))
        self.assertEqual(caught.exception.status_code, 400)
