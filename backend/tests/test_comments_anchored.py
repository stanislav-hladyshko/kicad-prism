"""The design-identity comment route (ADR scripting-and-mcp §10 D16).

Each case drives the real endpoint function with a fixture bundle standing in
for the revision's projections, so the component, net and sheet resolution runs
for real while git, the bundle store and the comment store are patched at their
seams. The browser sends the same ``location``/element fields when a person
clicks the item.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from app.api import comments_anchored
from app.core.security import AuthenticatedUser
from app.services import comment_design_anchor_service as anchors
from app.services.comment_anchor_service import ResolvedAnchor
from app.services.design_bundle.errors import DesignDataPending

COMMIT = "a" * 40
PROJECT = SimpleNamespace(id="p1", path="/repo/board", project_file="plain.kicad_pro")

PLACEMENTS = {
    "footprints": [
        {
            "uuid": "fp-r1",
            "reference": "R1",
            "side": "front",
            "positionNm": {"xNm": 10_000_000, "yNm": 20_000_000},
            "rotationDeg": 0,
            "courtyardBounds": {
                "minNm": {"xNm": -1_000_000, "yNm": -500_000},
                "maxNm": {"xNm": 1_000_000, "yNm": 500_000},
            },
            "pads": [
                {
                    "number": "1",
                    "positionNm": {"xNm": 9_000_000, "yNm": 20_000_000},
                    "size": {"xNm": 2_000_000, "yNm": 1_000_000},
                }
            ],
        }
    ]
}

BOARD_OBJECTS = {
    "documents": [
        {
            "path": "board/plain.kicad_sch",
            "objects": [
                {
                    "kind": "symbol",
                    "uuid": "sym-r1",
                    "refdes": "R1",
                    "at": {"xNm": 20_000_000, "yNm": 20_000_000},
                },
                {
                    "kind": "pin",
                    "uuid": "pin-r1-1",
                    "parentUuid": "sym-r1",
                    "number": "1",
                    "refdes": "R1",
                    "at": {"xNm": 30_000_000, "yNm": 30_000_000},
                },
            ],
        }
    ]
}

SEMANTIC_INDEX = {
    "components": [
        {
            "componentUid": "cmp:r1",
            "reference": "R1",
            "schematicRefs": [
                {"sheetInstancePath": "/", "page": "plain.kicad_sch", "symbolUuid": "sym-r1"}
            ],
            "pcbRefs": [{"footprintUuid": "fp-r1"}],
        }
    ],
    "nets": [{"name": "N1", "netUid": "net:n1"}],
    "terminals": [
        {
            "reference": "R1",
            "pin": "1",
            "netUid": "net:n1",
            "netName": "N1",
            "schematicPinUuid": "pin-r1-1",
            "pcbPadUuid": "pad-r1-1",
        }
    ],
    "sheetInstances": [
        {
            "sheetInstanceUid": "sheet-instance:root",
            "sheetInstancePath": "/",
            "sheetPath": "/",
            "page": "plain.kicad_sch",
            "sheetName": "plain",
            "sheetSymbolUuid": None,
        }
    ],
}


class FakeRead:
    """The two members ``resolve_anchor`` reads, in a ``BundleRead``'s shape."""

    def __init__(self, documents: dict | None = None) -> None:
        self._documents = documents if documents is not None else {
            anchors.SEMANTIC_INDEX: SEMANTIC_INDEX,
            anchors.PLACEMENTS: PLACEMENTS,
            anchors.BOARD_OBJECTS: BOARD_OBJECTS,
        }

    def ready(self, path: str) -> bool:
        return path in self._documents

    def json(self, path: str):
        return self._documents[path]


def canvas_anchor() -> ResolvedAnchor:
    return ResolvedAnchor(
        state="pinned",
        source="client",
        commit=COMMIT,
        source_revision_key="revision-key",
        file_path="board/plain.kicad_pro",
        project_relative_path="board/plain.kicad_pro",
        design_key="design-key",
    )


def user() -> AuthenticatedUser:
    return AuthenticatedUser(
        email="agent@example.test",
        name="Agent",
        role="designer",
        auth_type="session",
        user_id="user-1",
    )


def request(**values) -> comments_anchored.AnchoredCommentRequest:
    return comments_anchored.AnchoredCommentRequest(
        content=values.pop("content", "Please check this."), **values
    )


class AnchoredCommentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(
            mock.patch.object(
                comments_anchored, "get_project_for_role_or_404", return_value=PROJECT
            )
        )
        self.enterContext(
            mock.patch.object(comments_anchored.design_revision, "resolve", return_value=COMMIT)
        )
        self.enterContext(
            mock.patch.object(
                comments_anchored, "resolve_canvas_anchor", return_value=canvas_anchor()
            )
        )
        self.enterContext(
            mock.patch.object(comments_anchored.comments_store, "create_comment", return_value={"id": "c1"})
        )
        self.create = comments_anchored.comments_store.create_comment

    def _bundle(self, documents: dict | None = None) -> None:
        self.enterContext(
            mock.patch.object(anchors.consumer, "bundle_read", return_value=FakeRead(documents))
        )

    def _create(self, body: comments_anchored.AnchoredCommentRequest):
        result = asyncio.run(comments_anchored.create_anchored_comment("p1", body, user()))
        self.assertEqual(result, {"id": "c1"})
        return self.create.call_args.kwargs

    def test_a_component_in_the_board_view_anchors_at_the_footprint_centre(self) -> None:
        self._bundle()
        created = self._create(request(component="R1"))
        self.assertEqual(created["context"], "PCB")
        self.assertEqual(created["location"], {"x": 10.0, "y": 20.0, "layer": "F.Cu", "page": ""})
        self.assertEqual(
            (created["element_id"], created["element_ref"], created["element_type"]),
            ("fp-r1", "R1", "footprint"),
        )
        self.assertEqual(created["anchor_commit"], COMMIT)
        self.assertEqual(created["file_path"], "board/plain.kicad_pro")
        self.assertEqual(created["anchor_design_key"], "design-key")
        self.assertEqual(created["project_id"], "p1")

    def test_a_component_in_the_schematic_view_anchors_at_the_symbol_centre(self) -> None:
        self._bundle()
        created = self._create(request(component="R1", view="sch"))
        self.assertEqual(created["context"], "SCH")
        self.assertEqual(created["location"], {"x": 25.0, "y": 25.0, "layer": "", "page": "plain.kicad_sch"})
        self.assertEqual(
            (created["element_id"], created["element_ref"], created["element_type"]),
            ("sym-r1", "R1", "symbol"),
        )

    def test_a_net_in_the_board_view_anchors_at_its_first_pad(self) -> None:
        self._bundle()
        created = self._create(request(net="N1"))
        self.assertEqual(created["context"], "PCB")
        self.assertEqual(created["location"], {"x": 9.0, "y": 20.0, "layer": "F.Cu", "page": ""})
        self.assertEqual(
            (created["element_id"], created["element_ref"], created["element_type"]),
            ("pad-r1-1", "N1", "net"),
        )

    def test_a_net_in_the_schematic_view_anchors_at_its_first_pin(self) -> None:
        self._bundle()
        created = self._create(request(net="N1", view="sch"))
        self.assertEqual(created["context"], "SCH")
        self.assertEqual(created["location"], {"x": 30.0, "y": 30.0, "layer": "", "page": "plain.kicad_sch"})
        self.assertEqual(
            (created["element_id"], created["element_ref"], created["element_type"]),
            ("pin-r1-1", "N1", "net"),
        )

    def test_a_sheet_anchors_at_the_page_origin(self) -> None:
        self._bundle()
        created = self._create(request(sheet="/"))
        self.assertEqual(created["context"], "SCH")
        self.assertEqual(created["location"], {"x": 0.0, "y": 0.0, "layer": "", "page": "/"})
        self.assertEqual(
            (created["element_id"], created["element_ref"], created["element_type"]),
            (None, "/", "sheet"),
        )

    def test_an_unknown_component_is_anchor_not_found(self) -> None:
        self._bundle()
        with self.assertRaises(anchors.AnchorNotFound) as caught:
            self._create(request(component="R404"))
        self.assertEqual((caught.exception.status_code, caught.exception.code), (404, "anchor-not-found"))

    def test_an_unknown_net_or_sheet_is_anchor_not_found(self) -> None:
        self._bundle()
        for body in (request(net="NOPE"), request(sheet="/NOPE/")):
            with self.subTest(body=body):
                with self.assertRaises(anchors.AnchorNotFound):
                    self._create(body)

    def test_no_anchor_or_several_are_invalid(self) -> None:
        self._bundle()
        for body in (request(), request(component="R1", net="N1")):
            with self.subTest(body=body):
                with self.assertRaises(anchors.InvalidAnchor) as caught:
                    self._create(body)
                self.assertEqual((caught.exception.status_code, caught.exception.code), (400, "invalid-anchor"))

    def test_a_sheet_cannot_be_asked_for_on_the_board(self) -> None:
        self._bundle()
        with self.assertRaises(anchors.InvalidAnchor):
            self._create(request(sheet="/", view="pcb"))

    def test_an_unbuilt_bundle_is_bundle_not_ready(self) -> None:
        self.enterContext(
            mock.patch.object(
                anchors.consumer,
                "bundle_read",
                side_effect=DesignDataPending([anchors.SEMANTIC_INDEX]),
            )
        )
        with self.assertRaises(anchors.BundleNotReady) as caught:
            self._create(request(component="R1"))
        self.assertEqual((caught.exception.status_code, caught.exception.code), (409, "bundle-not-ready"))

    def test_an_unknown_revision_is_revision_unknown(self) -> None:
        self._bundle()
        with mock.patch.object(
            comments_anchored.design_revision, "resolve", side_effect=ValueError("unknown")
        ):
            with self.assertRaises(comments_anchored.BadRequest) as caught:
                self._create(request(component="R1"))
        self.assertEqual(caught.exception.code, "revision-unknown")


if __name__ == "__main__":
    unittest.main()
