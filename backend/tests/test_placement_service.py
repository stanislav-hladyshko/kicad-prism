"""Pick-and-place and BOM parsing and the cross-check."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.services.placement_service import (
    PlacementError,
    build_view,
    natural_key,
    parse_bom,
    parse_positions,
    position_files,
    view_of,
)

KICAD_POS = (
    "Ref,Val,Package,PosX,PosY,Rot,Side\n"
    '"C1","10nF","C_0402",120.5,-110.25,90.0,top\n'
    '"R10","1k","R_0402",130.0,-96.5,0.0,bottom\n'
    '"R2","1k","R_0402",131.0,-96.5,270.0,top\n'
    '"FID1","","Fiducial",101.0,-76.0,0.0,top\n'
)
KICAD_BOM = (
    '"Refs","Value","Footprint","Qty","DNP"\n'
    '"C1","10nF","C_0402","1",""\n'
    '"R2, R10","1k","R_0402","2",""\n'
    '"R5","1k","R_0402","1",""\n'
    '"C9","1uF","C_0402","1","DNP"\n'
)
JLC_CPL = (
    "Designator,Val,Package,Mid X,Mid Y,Rotation,Layer\n"
    "C1,10nF,C1206,120.9611mm,-110.9036mm,180.0,Bottom\n"
    "C2,,C1206,130.5011,-96.8236,270.0,Top\n"
)
JLC_BOM = (
    "Comment,Designator,Footprint,LCSC,Quantity\n"
    "10nF,C1,C1206,C1525,1\n"
    ",C2,C1206,,1\n"
)


class ParsePositionsTests(unittest.TestCase):
    def test_kicad_columns(self) -> None:
        parts, warnings = parse_positions(KICAD_POS)
        self.assertEqual(warnings, [])
        c1 = parts[0]
        self.assertEqual((c1.ref, c1.value, c1.package), ("C1", "10nF", "C_0402"))
        self.assertEqual((c1.x, c1.y, c1.rotation, c1.side), (120.5, -110.25, 90.0, "top"))

    def test_jlcpcb_columns_with_units_and_capitalised_sides(self) -> None:
        parts, _ = parse_positions(JLC_CPL)
        self.assertEqual((parts[0].x, parts[0].y, parts[0].side), (120.9611, -110.9036, "bottom"))
        self.assertEqual(parts[1].side, "top")

    def test_other_delimiters_and_a_byte_order_mark(self) -> None:
        text = "﻿Ref;PosX;PosY;Rot;Side\nC1;1,0;2;0;top\n".replace("1,0", "1.0")
        parts, _ = parse_positions(text)
        self.assertEqual((parts[0].x, parts[0].y), (1.0, 2.0))

    def test_inches_and_mils_become_millimetres(self) -> None:
        parts, _ = parse_positions("Ref,PosX,PosY\nC1,1in,500mil\n")
        self.assertAlmostEqual(parts[0].x, 25.4)
        self.assertAlmostEqual(parts[0].y, 12.7)

    def test_rotation_is_wrapped_into_a_turn(self) -> None:
        parts, _ = parse_positions("Ref,PosX,PosY,Rot\nC1,0,0,-90\nC2,0,0,450\n")
        self.assertEqual([part.rotation for part in parts], [270.0, 90.0])

    def test_a_bad_row_is_skipped_with_a_warning_not_a_failure(self) -> None:
        parts, warnings = parse_positions("Ref,PosX,PosY\nC1,1,2\nC2,oops,3\n")
        self.assertEqual([part.ref for part in parts], ["C1"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("C2", warnings[0])

    def test_a_repeated_reference_is_flagged(self) -> None:
        _, warnings = parse_positions("Ref,PosX,PosY\nC1,1,2\nC1,3,4\n")
        self.assertTrue(any("more than once" in warning for warning in warnings))

    def test_a_file_that_is_not_positions_is_refused(self) -> None:
        for text in ("", "Comment,Qty\nfoo,1\n", "Ref,PosX,PosY\n"):
            with self.subTest(text), self.assertRaises(PlacementError):
                parse_positions(text)


class ParseBomTests(unittest.TestCase):
    def test_grouped_references_and_dnp(self) -> None:
        lines = parse_bom(KICAD_BOM)
        self.assertEqual(lines[1].refs, ("R2", "R10"))
        self.assertTrue(lines[3].dnp)
        self.assertFalse(lines[0].dnp)

    def test_jlcpcb_bom(self) -> None:
        lines = parse_bom(JLC_BOM)
        self.assertEqual([line.refs for line in lines], [("C1",), ("C2",)])
        self.assertEqual(lines[0].value, "10nF")

    def test_other_dnp_spellings(self) -> None:
        for spelling in ("Yes", "TRUE", "x", "Do Not Place"):
            with self.subTest(spelling):
                self.assertTrue(parse_bom(f"Reference,Value,DNP\nC1,1u,{spelling}\n")[0].dnp)
        self.assertFalse(parse_bom("Reference,Value,DNP\nC1,1u,\n")[0].dnp)

    def test_a_file_that_is_not_a_bom_is_refused(self) -> None:
        with self.assertRaises(PlacementError):
            parse_bom("Ref,PosX,PosY\nC1,1,2\n".replace("Ref", "Thing"))


class BuildViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.view = build_view(KICAD_POS, KICAD_BOM, positions_name="pos.csv", bom_name="bom.csv")
        self.by_ref = {part["ref"]: part for part in self.view["parts"]}

    def test_parts_are_in_board_coordinates(self) -> None:
        # Position files are Y-up; the viewer's board frame is Y-down.
        self.assertEqual((self.by_ref["C1"]["x"], self.by_ref["C1"]["y"]), (120.5, 110.25))

    def test_parts_are_in_natural_order(self) -> None:
        self.assertEqual([part["ref"] for part in self.view["parts"]], ["C1", "FID1", "R2", "R10"])

    def test_status_of_each_placed_part(self) -> None:
        self.assertEqual(self.by_ref["C1"]["status"], "ok")
        self.assertEqual(self.by_ref["R10"]["status"], "ok")
        self.assertEqual(self.by_ref["FID1"]["status"], "not-in-bom")

    def test_bom_parts_that_were_never_placed(self) -> None:
        self.assertEqual([item["ref"] for item in self.view["missing"]], ["R5"])
        self.assertEqual(self.view["missing"][0]["status"], "not-placed")

    def test_a_dnp_part_that_is_not_placed_is_correct_not_missing(self) -> None:
        self.assertNotIn("C9", [item["ref"] for item in self.view["missing"]])

    def test_a_dnp_part_that_is_placed_is_flagged(self) -> None:
        view = build_view(KICAD_POS + '"C9","1uF","C_0402",1,-1,0,top\n', KICAD_BOM)
        row = next(part for part in view["parts"] if part["ref"] == "C9")
        self.assertEqual(row["status"], "dnp-placed")
        self.assertEqual(view["counts"]["dnpPlaced"], 1)

    def test_counts(self) -> None:
        self.assertEqual(
            self.view["counts"],
            {"placed": 4, "top": 3, "bottom": 1, "notInBom": 1, "dnpPlaced": 0, "notPlaced": 1},
        )
        self.assertEqual(self.view["files"], {"positions": "pos.csv", "bom": "bom.csv"})

    def test_without_a_bom_nothing_is_checked(self) -> None:
        view = build_view(KICAD_POS)
        self.assertFalse(view["hasBom"])
        self.assertEqual({part["status"] for part in view["parts"]}, {"no-bom"})
        self.assertEqual(view["missing"], [])
        self.assertEqual(view["files"]["bom"], "")

    def test_an_unreadable_bom_is_a_warning_and_the_positions_still_show(self) -> None:
        view = build_view(KICAD_POS, "nonsense,columns\n1,2\n")
        self.assertFalse(view["hasBom"])
        self.assertEqual(len(view["parts"]), 4)
        self.assertTrue(any("BOM not used" in warning for warning in view["warnings"]))

    def test_jlcpcb_files_check_cleanly(self) -> None:
        view = build_view(JLC_CPL, JLC_BOM)
        self.assertEqual(view["counts"]["notInBom"], 0)
        self.assertEqual(view["counts"]["notPlaced"], 0)


class NaturalKeyTests(unittest.TestCase):
    def test_numbers_sort_as_numbers(self) -> None:
        refs = ["R10", "R2", "C1", "R1"]
        self.assertEqual(sorted(refs, key=natural_key), ["C1", "R1", "R2", "R10"])


class ReferenceRangeTests(unittest.TestCase):
    def test_ranges_expand_in_a_bom_cell(self) -> None:
        for cell in ("R1-R4", "R1-4", "R1 - R4", "R1, R2-R3, R4"):
            with self.subTest(cell):
                lines = parse_bom(f'Refs,Value\n"{cell}",1k\n')
                self.assertEqual(lines[0].refs, ("R1", "R2", "R3", "R4"))

    def test_a_range_is_checked_against_the_positions(self) -> None:
        pos = "Ref,PosX,PosY\nR1,0,0\nR2,0,0\nR3,0,0\n"
        view = build_view(pos, "Refs,Value\nR1-R3,1k\n")
        self.assertEqual(view["counts"]["notInBom"], 0)
        self.assertEqual(view["counts"]["notPlaced"], 0)

    def test_a_backwards_or_runaway_range_is_left_alone(self) -> None:
        self.assertEqual(parse_bom("Refs,Value\nR4-R1,1k\n")[0].refs, ("R4-R1",))
        self.assertEqual(parse_bom("Refs,Value\nR1-R99999,1k\n")[0].refs, ("R1-R99999",))

    def test_hyphenated_names_that_are_not_ranges_survive(self) -> None:
        self.assertEqual(parse_bom("Refs,Value\nTP-1,x\n")[0].refs, ("TP-1",))


class PreambleTests(unittest.TestCase):
    def test_a_title_block_above_the_header_is_skipped(self) -> None:
        text = "Pick and place report\nGenerated by something\n\nRef,PosX,PosY,Side\nC1,1,2,top\n"
        parts, _ = parse_positions(text)
        self.assertEqual([part.ref for part in parts], ["C1"])

    def test_a_preamble_above_a_bom_is_skipped(self) -> None:
        lines = parse_bom("Project: board\nRevision: 2\nRefs,Value\nC1,1u\n")
        self.assertEqual(lines[0].refs, ("C1",))

    def test_a_file_with_no_header_anywhere_still_says_what_is_missing(self) -> None:
        with self.assertRaises(PlacementError) as caught:
            parse_positions("a,b\n1,2\n")
        self.assertIn("no ref", str(caught.exception))


class PositionFilesTests(unittest.TestCase):
    HEADER = "Ref,Val,Package,PosX,PosY,Rot,Side\n"

    def test_a_split_export_is_read_together_and_a_duplicate_is_not(self) -> None:
        top = self.HEADER + "C1,1u,C_0402,1,-1,0,top\n"
        bottom = self.HEADER + "R1,1k,R_0402,2,-2,0,bottom\n"
        # The caller passes the files that parsed as position files, in its order.
        candidates = [(name, parse_positions(text)[0]) for name, text in (("top", top), ("bottom", bottom), ("cpl", JLC_CPL))]
        self.assertEqual(position_files(candidates), ["top", "bottom"])

    def test_view_of_takes_parsed_files_and_no_bom(self) -> None:
        view = view_of([parse_positions(KICAD_POS)], None)
        self.assertFalse(view["hasBom"])
        self.assertEqual(view["counts"]["placed"], 4)
        self.assertEqual(view, build_view(KICAD_POS))

    def test_several_texts_make_one_view(self) -> None:
        top = self.HEADER + "C1,1u,C_0402,1,-1,0,top\n"
        bottom = self.HEADER + "R1,1k,R_0402,2,-2,0,bottom\n"
        view = build_view([top, bottom])
        self.assertEqual([part["ref"] for part in view["parts"]], ["C1", "R1"])


class DocumentPipelinePlacementsTests(unittest.TestCase):
    """Release Studio's sheets read the position file with the same parser."""

    def _placements(self, text: str | None):
        from app.release_studio.document_pipeline import _placements

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "positions.csv"
            if text is not None:
                path.write_text(text, encoding="utf-8")
            return _placements(path)

    def test_kicad_and_jlcpcb_files_give_the_same_shape(self) -> None:
        kicad = self._placements(KICAD_POS)
        self.assertEqual([(item["ref"], item["side"]) for item in kicad][:2], [("C1", "top"), ("R10", "bottom")])
        jlc = self._placements(JLC_CPL)
        self.assertEqual([(item["ref"], item["side"]) for item in jlc], [("C1", "bottom"), ("C2", "top")])

    def test_a_missing_or_unreadable_file_is_no_placements(self) -> None:
        self.assertEqual(self._placements(None), [])
        self.assertEqual(self._placements("a,b\n1,2\n"), [])


class PlacementResponseModelTests(unittest.TestCase):
    """The typed route answers exactly the dict ``view_of`` already built."""

    def test_the_model_serializes_the_view_unchanged(self) -> None:
        from fastapi.encoders import jsonable_encoder

        from app.api import fabrication_view_models as models

        for view in (
            build_view(KICAD_POS, KICAD_BOM, positions_name="pos.csv", bom_name="bom.csv"),
            build_view(KICAD_POS),
        ):
            with self.subTest(has_bom=view["hasBom"]):
                typed = models.PlacementView.model_validate(view).model_dump(
                    mode="json", by_alias=True, exclude_unset=True
                )
                self.assertEqual(typed, jsonable_encoder(view))


if __name__ == "__main__":
    unittest.main()
