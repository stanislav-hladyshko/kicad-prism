"""``render_view`` end to end: a real bundle, kicad-cli, and PNG evidence.

The fixture project is the one-resistor ``design_variants/no_variants`` design.
Its bundle is built by the same stub job the other design-bundle tests use; the
stub's projection documents are replaced with equivalents for that board so the
highlight boxes land on the plotted artwork. kicad-cli must be installed; the
test skips without it (and without cairosvg's native Cairo).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.core.config import settings
from app.services import event_log, kicad_tools
from app.services.design_bundle import jobs, rows
from app.services.design_render import service
from app.services.job_runtime import JobContext
from app.services.workspace_schema_migrations import WORKSPACE_LEDGER
from app.services.workspace_service import workspace
from in_memory_jobs import InMemoryJobService
from support.design_bundle_fixtures import install_fake_kicad, install_stub, project_repo
from support.postgres import requires_scratch_postgres, scratch_schema, seed_project, seed_repository

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "design_variants" / "no_variants"
EXAMPLES = Path(__file__).resolve().parents[2] / "native" / "contracts" / "prism" / "examples"
EVIDENCE = Path(
    "/private/tmp/claude-503/-Users-Swaroop-Personal-Projects-KiCAD-Platform/"
    "2ae8fce7-323f-4320-af7b-fc45e741e7f3/scratchpad/render-evidence"
)
SYMBOL_UUID = "b66904c4-ce01-5338-8a05-f248bd2fefae"
FOOTPRINT_UUID = "88322bda-6cf9-5219-94c9-e6fbd6d08eb1"
PIN_UUIDS = ("e5c655ea-1d3b-51a9-a457-5d995b5198ad", "ce97160b-b6a7-57c0-94ee-da11a7ac65f8")


def _examples() -> dict[str, bytes]:
    """The stub's example set with projections that describe the fixture board."""

    placements = json.loads((EXAMPLES / "PlacementsA1.example.json").read_text())
    template = placements["footprints"][0]
    template.update(
        uuid=FOOTPRINT_UUID,
        reference="R1",
        libId="Resistor_SMD:R_0603_1608Metric",
        side="front",
        positionNm={"xNm": 5_000_000, "yNm": 5_000_000},
        rotationDeg=0,
        courtyardBounds={
            "minNm": {"xNm": -1_500_000, "yNm": -1_000_000},
            "maxNm": {"xNm": 1_500_000, "yNm": 1_000_000},
        },
        pads=[
            {
                "number": "1",
                "type": "smd",
                "shape": "rect",
                "positionNm": {"xNm": 4_000_000, "yNm": 5_000_000},
                "size": {"xNm": 800_000, "yNm": 900_000},
            },
            {
                "number": "2",
                "type": "smd",
                "shape": "rect",
                "positionNm": {"xNm": 6_000_000, "yNm": 5_000_000},
                "size": {"xNm": 800_000, "yNm": 900_000},
            },
        ],
        models=[],
    )
    placements.update(
        footprints=[template],
        designators=["R1"],
        outlineBounds={
            "minNm": {"xNm": 0, "yNm": 0},
            "maxNm": {"xNm": 60_000_000, "yNm": 40_000_000},
        },
    )

    board_objects = json.loads((EXAMPLES / "BoardObjectsA0.example.json").read_text())
    symbol = next(
        item
        for document in board_objects["documents"]
        for item in document["objects"]
        if item["kind"] == "symbol"
    )
    symbol = json.loads(json.dumps(symbol))
    symbol.update(
        uuid=SYMBOL_UUID,
        refdes="R1",
        at={"xNm": 20_000_000, "yNm": 20_000_000},
        rotationDeg=0,
        kiidPaths=[],
        instances=[{"reference": "R1", "path": f"/{SYMBOL_UUID}"}],
    )
    pin = next(
        item
        for document in board_objects["documents"]
        for item in document["objects"]
        if item["kind"] == "pin"
    )
    pins = []
    for index, uuid in enumerate(PIN_UUIDS, start=1):
        pins.append(
            {
                **{key: value for key, value in pin.items() if key in {"contentDigest", "reviewFields"}},
                "kind": "pin",
                "uuid": uuid,
                "parentUuid": SYMBOL_UUID,
                "number": str(index),
                "refdes": "R1",
                "at": {"xNm": 20_000_000, "yNm": 20_000_000},
                "reviewFields": {"Pin number": str(index)},
            }
        )
    board_objects = {
        "schema": "prism.board_objects.a0",
        "documents": [
            {
                "path": "board/plain.kicad_sch",
                "objects": [symbol, *pins],
                "reviewOnlyObjects": [],
            }
        ],
    }

    index = json.loads((EXAMPLES / "SemanticIndexA2.example.json").read_text())
    index.update(
        components=[
            {
                "componentUid": "cmp:r1",
                "reference": "R1",
                "value": "10k",
                "footprint": "Resistor_SMD:R_0603_1608Metric",
                "libraryRef": "Device:R",
                "fields": {},
                "schematicRefs": [
                    {
                        "sheetInstancePath": "/",
                        "page": "1",
                        "symbolUuid": SYMBOL_UUID,
                        "occurrenceId": f"/{SYMBOL_UUID}",
                    }
                ],
                "pcbRefs": [{"footprintUuid": FOOTPRINT_UUID}],
                "webgpuRefs": [],
            }
        ],
        terminals=[
            {
                "terminalUid": f"term:{index}",
                "componentUid": "cmp:r1",
                "reference": "R1",
                "pin": str(index),
                "pinName": "~",
                "pinType": "PASSIVE",
                "netUid": "net:n1",
                "netName": "N1",
                "schematicNetName": "N1",
            }
            for index in (1, 2)
        ],
        nets=[
            {
                "netUid": "net:n1",
                "name": "N1",
                "netClass": "Default",
                "aliases": [],
                "sourceSheets": ["/"],
                "schematicRefs": [],
                "pcbRefs": [],
                "webgpuRefs": [],
            }
        ],
        indexes={},
        sheetInstances=[],
        buses=[],
    )

    overrides = {
        "PlacementsA1": placements,
        "BoardObjectsA0": board_objects,
        "SemanticIndexA2": index,
    }
    files: dict[str, bytes] = {}
    for source in EXAMPLES.glob("*.example.json"):
        name = source.name[: -len(".example.json")]
        files[source.name] = (
            json.dumps(overrides[name], separators=(",", ":")).encode("utf-8")
            if name in overrides
            else source.read_bytes()
        )
    return files


def _kicad_available() -> bool:
    if kicad_tools.locate_kicad_cli() is None:
        return False
    try:
        import cairosvg  # noqa: F401
    except Exception:  # noqa: BLE001 - native Cairo missing is an environment skip
        return False
    return True


@unittest.skipUnless(_kicad_available(), "kicad-cli and cairosvg are required")
@requires_scratch_postgres("the render integration test")
class RenderIntegrationTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.scratch = scratch_schema(self, "design_render", ledgers=(WORKSPACE_LEDGER,))
        self.enterContext(mock.patch.object(rows, "default_connect", self.scratch.connection))
        self.enterContext(mock.patch.object(event_log, "connect", self.scratch.connection))
        self.enterContext(mock.patch.object(workspace, "_connect", self.scratch.connection))
        self.enterContext(mock.patch.object(settings, "KICAD_PROJECTS_ROOT", str(self.tmp / "projects")))
        self.enterContext(mock.patch.object(settings, "PRISM_DESIGN_BUNDLE_ROOT", ""))
        self.queue = InMemoryJobService()
        self.enterContext(mock.patch("app.services.job_service.jobs", self.queue))
        self.examples = self.tmp / "examples"
        self.examples.mkdir()
        for name, data in _examples().items():
            (self.examples / name).write_bytes(data)
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {"PRISM_KICAD_STUB_EXAMPLES": str(self.examples)},
            )
        )
        self.stub_log = install_stub(self)
        install_fake_kicad(self)
        # The bundle build may use the fake KiCad Python, but the render itself
        # must run the real kicad-cli (the fake accepts only upgrade and GLB).
        self.enterContext(mock.patch.object(settings, "KICAD_CLI_PATH", ""))
        self.repo = self._project_repo()

    def _project_repo(self) -> Path:
        files = {
            "board/plain.kicad_pro": (FIXTURE / "plain.kicad_pro").read_text(),
            "board/plain.kicad_sch": (FIXTURE / "plain.kicad_sch").read_text(),
            "board/plain.kicad_pcb": (FIXTURE / "plain.kicad_pcb").read_text(),
        }
        repo = project_repo(self.tmp / "projects" / "clones" / "r1", files)
        with self.scratch.connection() as conn:
            seed_repository(conn, "r1", clone_path="clones/r1")
            seed_project(
                conn, "p1", "r1", relative_path="board", project_file_rel="plain.kicad_pro"
            )
        return repo

    def _run(self, job_id: str):
        claimed = self.queue.claim("test-worker", worker_pool="prism")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["job_id"], job_id)
        from app.services import job_runtime

        with mock.patch.object(job_runtime, "job_state_root", return_value=self.tmp / "state"):
            return JobContext(claimed, worker_id="test-worker", service=self.queue)

    def _build_bundle(self) -> None:
        job_id = jobs.start_design_bundle_job("p1", requested_by="test")
        self.assertIsNotNone(job_id)
        jobs.run_design_bundle_job(self._run(job_id))

    def _render(self, request: dict) -> tuple[dict, Path]:
        project = SimpleNamespace(
            id="p1", path=str(self.repo / "board"), project_file="plain.kicad_pro"
        )
        started = service.start_render(project, request)
        if started["status"] != "completed":
            service.run_render_view_job(self._run(started["jobId"]))
        record = service.get_render(project, started["renderId"])
        self.assertIsNotNone(record)
        self.assertEqual(record["status"], "completed", record)
        png = service.png_path(project, started["renderId"])
        self.assertIsNotNone(png)
        return record, png

    def _evidence(self, name: str, png: Path) -> None:
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(png, EVIDENCE / name)

    def _assert_overlay(self, highlighted: Path, plain: Path, region: dict, box, width: int) -> None:
        from PIL import Image

        scale_x = width / region["width"]
        scale_y = round(width * region["height"] / region["width"]) / region["height"]
        left = round((box.min_x - region["x"]) * scale_x)
        top = round((box.min_y - region["y"]) * scale_y)
        right = round((box.max_x - region["x"]) * scale_x)
        bottom = round((box.max_y - region["y"]) * scale_y)
        with Image.open(highlighted) as first, Image.open(plain) as second:
            first = first.convert("RGBA")
            second = second.convert("RGBA")
            changed = 0
            ink = 0
            for y in range(max(0, top), min(first.height, bottom)):
                for x in range(max(0, left), min(first.width, right)):
                    a = first.getpixel((x, y))
                    b = second.getpixel((x, y))
                    if a != b:
                        changed += 1
                    if sum(b[:3]) < 700:
                        ink += 1
        self.assertGreater(changed, 50, "the overlay did not change the expected box")
        self.assertGreater(ink, 50, "the expected box is not over artwork")

    def test_a_schematic_component_render_and_overlay(self) -> None:
        self._build_bundle()
        region = {"x": 14, "y": 14, "width": 12, "height": 12}
        record, png = self._render(
            {
                "view": "schematic",
                "highlight": {"components": ["R1"]},
                "region": region,
                "width": 640,
            }
        )
        self.assertEqual((record["width"], record["height"]), (640, 640))
        self.assertEqual(record["sheet"], "1")
        self.assertEqual(record["highlights"], [{"kind": "component", "key": "R1", "found": True}])
        self._evidence("schematic-component.png", png)

        _, plain = self._render(
            {"view": "schematic", "region": region, "width": 640}
        )
        box = {
            "min_x": 17.46,
            "min_y": 17.46,
            "max_x": 22.54,
            "max_y": 22.54,
        }
        self._assert_overlay(png, plain, region, SimpleNamespace(**box), 640)

    def test_a_pcb_component_render_and_overlay(self) -> None:
        self._build_bundle()
        region = {"x": 2.5, "y": 2.5, "width": 5, "height": 5}
        record, png = self._render(
            {
                "view": "pcb",
                "layers": "front",
                "highlight": {"components": ["R1"]},
                "region": region,
                "width": 640,
            }
        )
        self.assertEqual((record["width"], record["height"]), (640, 640))
        self.assertEqual(record["highlights"], [{"kind": "component", "key": "R1", "found": True}])
        self._evidence("pcb-component.png", png)

        _, plain = self._render({"view": "pcb", "region": region, "width": 640})
        box = SimpleNamespace(min_x=3.5, min_y=4.0, max_x=6.5, max_y=6.0)
        self._assert_overlay(png, plain, region, box, 640)

    def test_a_3d_render_has_no_overlay(self) -> None:
        self._build_bundle()
        record, png = self._render(
            {
                "view": "3d",
                "side": "top",
                "highlight": {"components": ["R1"], "nets": ["N1"]},
                "width": 640,
            }
        )
        self.assertEqual(record["width"], 640)
        self.assertEqual(record["height"], round(640 * 40 / 60))
        self.assertTrue(all(item["found"] for item in record["highlights"]))
        self._evidence("pcb-3d.png", png)
        with png.open("rb") as handle:
            self.assertEqual(handle.read(8), b"\x89PNG\r\n\x1a\n")

    def test_a_repeat_render_hits_the_cache(self) -> None:
        self._build_bundle()
        request = {"view": "pcb", "highlight": {"components": ["R1"]}, "width": 512}
        first = service.start_render(
            SimpleNamespace(id="p1", path=str(self.repo / "board"), project_file="plain.kicad_pro"),
            request,
        )
        service.run_render_view_job(self._run(first["jobId"]))
        second = service.start_render(
            SimpleNamespace(id="p1", path=str(self.repo / "board"), project_file="plain.kicad_pro"),
            request,
        )
        self.assertEqual(second, {"renderId": first["renderId"], "status": "completed"})


if __name__ == "__main__":
    unittest.main()
