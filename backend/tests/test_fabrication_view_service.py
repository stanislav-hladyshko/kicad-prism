"""Single-revision fabrication package view model."""

from __future__ import annotations

import json
import re
import unittest

from app.services.fabrication_view_service import (
    FabricationPackage,
    FabricationViewError,
    classify,
    is_drill_map,
)


def gerber(body: str) -> bytes:
    return (
        "%TF.GenerationSoftware,KiCad,Pcbnew,10.0.4*%\n"
        "%FSLAX46Y46*%\n"
        "%MOMM*%\n"
        "%LPD*%\n"
        "G01*\n"
        "%ADD10C,0.250000*%\n"
        "%ADD11R,1.000000X1.000000*%\n"
        "D10*\n"
        f"{body}"
        "M02*\n"
    ).encode()


def excellon(tools: str, body: str) -> bytes:
    return (
        "M48\n"
        "FMAT,2\n"
        "METRIC\n"
        f"{tools}"
        "%\n"
        "G90\n"
        "G05\n"
        f"{body}"
        "M30\n"
    ).encode()


#: A 20 x 10 mm rectangular profile, in KiCad's Gerber frame (Y up).
OUTLINE = (
    "X0Y0D02*\n"
    "X20000000Y0D01*\n"
    "X20000000Y10000000D01*\n"
    "X0Y10000000D01*\n"
    "X0Y0D01*\n"
)
PAD = "D11*\nX5000000Y5000000D03*\n"
# A mark well outside the 20 x 10 mm profile.
SILK_MARK = "D11*\nX30000000Y30000000D03*\n"

JOB = {
    "FilesAttributes": [
        {"Path": "board-F_Cu.gtl", "FileFunction": "Copper,L1,Top"},
        {"Path": "board-B_Cu.gbl", "FileFunction": "Copper,L2,Bot"},
        {"Path": "board-F_Mask.gts", "FileFunction": "SolderMask,Top"},
        {"Path": "board-F_Paste.gtp", "FileFunction": "SolderPaste,Top"},
        {"Path": "board-F_Silkscreen.gto", "FileFunction": "Legend,Top"},
        {"Path": "board-Edge_Cuts.gm1", "FileFunction": "Profile"},
        {"Path": "board-F_Fab.gbr", "FileFunction": "AssemblyDrawing,Top"},
    ]
}

DRILL = excellon(
    "; #@! TA.AperFunction,Plated,PTH,ViaDrill\nT1C0.300\n"
    "; #@! TA.AperFunction,Plated,PTH,ComponentDrill\nT2C0.800\n"
    "; #@! TA.AperFunction,NonPlated,NPTH,ComponentDrill\nT3C3.200\n",
    "T1\nX5.0Y-5.0\nX6.0Y-5.0\n"
    "T2\nX8.0Y-5.0\n"
    "T3\nX2.0Y-2.0\n",
)


def package_files() -> dict[str, bytes]:
    return {
        "board-F_Cu.gtl": gerber(PAD),
        "board-B_Cu.gbl": gerber(PAD),
        "board-F_Mask.gts": gerber(PAD),
        "board-F_Paste.gtp": gerber(PAD),
        "board-F_Silkscreen.gto": gerber(PAD),
        "board-Edge_Cuts.gm1": gerber(OUTLINE),
        "board-F_Fab.gbr": gerber(PAD),
        "board.drl": DRILL,
        "board-job.gbrjob": json.dumps(JOB).encode(),
    }


class ClassifyTests(unittest.TestCase):
    def test_x2_functions(self) -> None:
        cases = {
            "Copper,L1,Top": ("copper", "top"),
            "Copper,L2,Inr": ("copper", "inner"),
            "Copper,L4,Bot": ("copper", "bottom"),
            "SolderMask,Bot": ("mask", "bottom"),
            "SolderPaste,Top": ("paste", "top"),
            "Legend,Top": ("silk", "top"),
            "Profile": ("outline", "both"),
            "Other,User": ("other", "both"),
            "AssemblyDrawing,Bot": ("other", "bottom"),
        }
        for function, expected in cases.items():
            with self.subTest(function):
                self.assertEqual(classify(function, "gerber"), expected)

    def test_drill_is_by_grammar_not_function(self) -> None:
        self.assertEqual(classify("NCDrill", "excellon"), ("drill", "both"))

    def test_unknown_function_is_other_not_dropped(self) -> None:
        self.assertEqual(classify("Unknown,Mystery", "gerber"), ("other", "both"))


class ClassifyByNameTests(unittest.TestCase):
    """Files with no declared function are read from their name."""

    def role(self, filename: str, name: str | None = None):
        stem = filename.rsplit(".", 1)[0]
        return classify(f"Unknown,{name or stem}", "gerber", filename, name or stem)

    def test_the_jlcpcb_plugin_names(self) -> None:
        cases = {
            "board-CuTop.gbr": ("copper", "top"),
            "board-CuBottom.gbr": ("copper", "bottom"),
            "board-MaskTop.gbr": ("mask", "top"),
            "board-MaskBottom.gbr": ("mask", "bottom"),
            "board-SilkTop.gbr": ("silk", "top"),
            "board-SilkBottom.gbr": ("silk", "bottom"),
            "board-EdgeCuts.gbr": ("outline", "both"),
        }
        for filename, expected in cases.items():
            with self.subTest(filename):
                self.assertEqual(self.role(filename, filename.split("-")[1].split(".")[0]), expected)

    def test_kicad_style_names_without_a_job_file(self) -> None:
        cases = {
            "F_Cu": ("copper", "top"),
            "B_Cu": ("copper", "bottom"),
            "In1_Cu": ("copper", "inner"),
            "In12_Cu": ("copper", "inner"),
            "F_Mask": ("mask", "top"),
            "B_Paste": ("paste", "bottom"),
            "F_Silkscreen": ("silk", "top"),
            "Edge_Cuts": ("outline", "both"),
        }
        for name, expected in cases.items():
            with self.subTest(name):
                self.assertEqual(self.role(f"board-{name}.gbr", name), expected)

    def test_protel_extensions_win_over_the_name(self) -> None:
        cases = {
            "x.GTL": ("copper", "top"),
            "x.gbl": ("copper", "bottom"),
            "x.gts": ("mask", "top"),
            "x.gbo": ("silk", "bottom"),
            "x.gtp": ("paste", "top"),
            "x.gm1": ("outline", "both"),
            "x.g2": ("copper", "inner"),
        }
        for filename, expected in cases.items():
            with self.subTest(filename):
                self.assertEqual(self.role(filename, "anything"), expected)

    def test_a_name_with_no_hint_stays_other(self) -> None:
        self.assertEqual(self.role("board-Courtyard.gbr", "Courtyard"), ("other", "both"))
        self.assertEqual(self.role("document.gbr", "document"), ("other", "both"))

    def test_a_declared_function_is_never_overridden(self) -> None:
        self.assertEqual(classify("Other,User", "gerber", "board-CuTop.gbr", "CuTop"), ("other", "both"))
        self.assertEqual(classify("Copper,L1,Top", "gerber", "board-SilkBottom.gbr", "SilkBottom"), ("copper", "top"))


class UnattributedPackageTests(unittest.TestCase):
    """A package from a plugin: no job file, nothing declared."""

    def test_roles_outline_and_size_come_from_the_names(self) -> None:
        files = {
            "board-CuTop.gbr": gerber(PAD),
            "board-CuBottom.gbr": gerber(PAD),
            "board-SilkTop.gbr": gerber(SILK_MARK),
            "board-EdgeCuts.gbr": gerber(OUTLINE),
            "board-PTH.drl": DRILL,
        }
        view = FabricationPackage.from_files(files).view()
        roles = {layer["file"]: (layer["role"], layer["side"]) for layer in view["layers"]}
        self.assertEqual(roles["board-CuTop.gbr"], ("copper", "top"))
        self.assertEqual(roles["board-EdgeCuts.gbr"], ("outline", "both"))
        self.assertEqual(view["copperLayers"], 2)
        # The board is the profile (20 x 10), not the silk mark outside it.
        self.assertEqual(view["size"], {"width": 20.0, "height": 10.0})


class DrillToolTests(unittest.TestCase):
    def tools(self, files):
        return FabricationPackage.from_files(files).view()["drill"]["tools"]

    def test_a_tool_that_is_defined_but_never_used_is_not_listed(self) -> None:
        text = excellon(
            "; #@! TA.AperFunction,Plated,PTH,ViaDrill\nT1C0.300\n"
            "; #@! TA.AperFunction,Plated,PTH,ComponentDrill\nT2C0.800\n",
            "T1\nX5.0Y-5.0\n",
        )
        self.assertEqual([tool["diameter"] for tool in self.tools({"board.drl": text})], [0.3])

    def test_a_drill_file_with_no_attributes_is_read_from_its_name(self) -> None:
        bare = excellon("T1C0.500\n", "T1\nX1.0Y-1.0\n")
        tools = self.tools({"board-PTH.drl": bare, "board-NPTH.drl": bare})
        by_file = {tool["file"]: tool["plated"] for tool in tools}
        self.assertEqual(by_file, {"board-PTH.drl": True, "board-NPTH.drl": False})

    def test_a_declared_function_beats_the_file_name(self) -> None:
        text = excellon("; #@! TA.AperFunction,Plated,PTH,ViaDrill\nT1C0.300\n", "T1\nX5.0Y-5.0\n")
        self.assertTrue(self.tools({"board-NPTH.drl": text})[0]["plated"])


class ColourTests(unittest.TestCase):
    """Layers are KiCad's colours, the same as the Visualizer draws them."""

    def setUp(self) -> None:
        view = FabricationPackage.from_files(package_files()).view()
        self.colours = {layer["id"]: layer["colour"] for layer in view["layers"]}

    def test_copper_is_red_on_top_and_blue_underneath(self) -> None:
        self.assertEqual(self.colours["f.cu"], "#c83434")
        self.assertEqual(self.colours["b.cu"], "#4d7fc4")

    def test_the_other_roles(self) -> None:
        self.assertEqual(self.colours["f.mask"], "#d864ff")
        self.assertEqual(self.colours["f.silkscreen"], "#f2eda1")
        self.assertEqual(self.colours["f.paste"], "#b4a09a")
        self.assertEqual(self.colours["edge.cuts"], "#d0d2cd")

    def test_a_fabrication_layer_has_its_own_colour(self) -> None:
        self.assertEqual(self.colours["f.fab"], "#afafaf")


class LayerOrderTests(unittest.TestCase):
    def test_inner_layers_list_in_board_order_past_nine(self) -> None:
        files = package_files()
        for index in (10, 2, 1):
            files[f"board-In{index}_Cu.g{index}"] = gerber(PAD)
        names = [
            layer["name"]
            for layer in FabricationPackage.from_files(files).view()["layers"]
            if layer["side"] == "inner"
        ]
        self.assertEqual([int("".join(filter(str.isdigit, name.split("_")[0]))) for name in names], [1, 2, 10])


class RenderVersionTests(unittest.TestCase):
    def test_the_view_names_the_rendering_its_layer_urls_carry(self) -> None:
        from app.services.fabrication_view_service import RENDER_VERSION

        view = FabricationPackage.from_files(package_files()).view()
        self.assertEqual(view["renderVersion"], RENDER_VERSION)


class OpaqueLayerTests(unittest.TestCase):
    """A layer is its colour, fully opaque, where plotted and transparent elsewhere."""

    def setUp(self) -> None:
        self.package = FabricationPackage.from_files(package_files())

    def cleared(self) -> FabricationPackage:
        files = package_files()
        files["board-F_Cu.gtl"] = gerber(
            "D11*\nX5000000Y5000000D03*\n%LPC*%\nD10*\nX5000000Y5000000D03*\n"
        )
        return FabricationPackage.from_files(files)

    def test_a_layer_with_nothing_cleared_is_drawn_in_its_colour_directly(self) -> None:
        # No mask: it costs the browser a second drawing of every shape.
        svg = self.package.svg("f.cu")
        self.assertNotIn("<mask", svg)
        self.assertIn('"#c83434"', svg)

    def test_nothing_is_painted_outside_the_plot(self) -> None:
        # No backdrop, black or otherwise: where nothing is plotted the layer is empty,
        # so the layers behind show there.
        for package in (self.package, self.cleared()):
            svg = package.svg("f.cu")
            outside = svg.split("</defs>")[-1]
            self.assertNotIn('"#000000"', outside)
            self.assertNotIn('"#ffffff"', outside)

    def test_no_layer_is_ever_see_through(self) -> None:
        for package in (self.package, self.cleared()):
            for layer in package.view()["layers"]:
                svg = package.svg(layer["id"])
                self.assertNotIn("opacity", svg, layer["id"])
                self.assertNotIn("mix-blend", svg, layer["id"])

    def test_every_layer_is_its_own_colour_and_nothing_else(self) -> None:
        for layer in self.package.view()["layers"]:
            svg = self.package.svg(layer["id"])
            outside = svg.split("</defs>")[-1]
            self.assertIn(f'"{layer["colour"]}"', outside, layer["id"])

    def test_a_clear_area_is_cut_through_a_mask(self) -> None:
        svg = self.cleared().svg("f.cu")
        self.assertIn('<mask id="layer"', svg)
        self.assertIn('fill="#c83434" mask="url(#layer)"', svg)
        mask = svg.split("<mask")[1].split("</mask>")[0]
        # Dark pad, then a clear flash over it: painted white, then black.
        self.assertLess(mask.index('fill="#ffffff"'), mask.rindex('fill="#000000"'))
        # The layer's own colour is never part of the mask.
        self.assertNotIn("#c83434", mask)


class MinimumStrokeTests(unittest.TestCase):
    """A thin line stays visible, in its own colour, when the whole board is in view."""

    def setUp(self) -> None:
        self.package = FabricationPackage.from_files(package_files())

    def test_a_drawn_line_is_never_thinner_than_the_minimum_on_screen(self) -> None:
        svg = self.package.svg("edge.cuts")
        self.assertIn('style="stroke-width:max(0.25px,var(--m,0px))"', svg)
        # The stroke's own width is still there, for anything that ignores the rule.
        self.assertIn('stroke-width="0.25"', svg)

    def test_the_svg_learns_its_drawn_size_from_media_queries(self) -> None:
        svg = self.package.svg("edge.cuts")
        rungs = re.findall(r"@media \(min-width:(\d+)px\)\{svg\{--m:([0-9.]+)px\}\}", svg)
        self.assertGreater(len(rungs), 10)
        widths = [int(width) for width, _ in rungs]
        self.assertEqual(widths, sorted(widths))
        # A wider drawing has a smaller pixel, so the minimum shrinks as you zoom in.
        pixels = [float(pixel) for _, pixel in rungs]
        self.assertEqual(pixels, sorted(pixels, reverse=True))

    def test_the_minimum_is_a_pixel_and_a_half_wherever_it_applies(self) -> None:
        svg = self.package.svg("edge.cuts")
        extent = float(svg.split('viewBox="')[1].split()[2])
        for width, pixel in re.findall(r"min-width:(\d+)px\)\{svg\{--m:([0-9.]+)px", svg):
            on_screen = float(pixel) * int(width) / extent
            self.assertAlmostEqual(on_screen, 1.5, delta=0.02)

    def test_a_pad_is_not_thickened(self) -> None:
        # Copper here is flashes only: shapes, not lines.
        svg = self.package.svg("f.cu")
        self.assertNotIn("<polyline", svg)
        self.assertNotIn("var(--m", svg.split("</style>")[1])

    def test_drill_holes_get_a_minimum_size_and_pads_do_not(self) -> None:
        drill = self.package.svg("drill")
        self.assertIn("style=\"r:max(0.15px,calc(var(--m,0px)/2))\"", drill)
        # A round pad on copper is true to size.
        files = package_files()
        files["board-F_Cu.gtl"] = gerber("%ADD12C,0.300000*%\nD12*\nX5000000Y5000000D03*\n")
        copper = FabricationPackage.from_files(files).svg("f.cu")
        self.assertIn("<circle", copper)
        self.assertNotIn("r:max", copper)

    def test_lines_inside_a_macro_keep_their_size(self) -> None:
        from app.services.fabrication_view_service import with_minimum_stroke

        svg = (
            '<svg viewBox="0 0 10 10"><line x1="0" y1="0" x2="1" y2="1" stroke="#fff" stroke-width="0.1"/>'
            '<polyline points="0,0 1,1" stroke="#fff" stroke-width="0.1"/></svg>'
        )
        out = with_minimum_stroke(svg, 10.0)
        self.assertEqual(out.count("var(--m"), 1)
        self.assertIn('<line x1="0" y1="0" x2="1" y2="1" stroke="#fff" stroke-width="0.1"/>', out)


def _names(files: dict[str, bytes]) -> list[str]:
    return sorted(layer["name"] for layer in FabricationPackage.from_files(files).view()["layers"])


class LayerNameTests(unittest.TestCase):
    def test_drill_programs_are_named_by_their_plating(self) -> None:
        files = {"board-PTH.drl": DRILL, "board-NPTH.drl": DRILL, "board-F_Cu.gtl": gerber(PAD)}
        self.assertEqual(_names(files), ["Drill (NPTH)", "Drill (PTH)", "F.Cu"])

    def test_without_a_job_file_the_shared_board_name_is_dropped(self) -> None:
        # A drill folder on its own: no job file names the board.
        files = {
            "USB-PD-Trigger-Board-PTH.drl": DRILL,
            "USB-PD-Trigger-Board-NPTH.drl": DRILL,
        }
        self.assertEqual(_names(files), ["Drill (NPTH)", "Drill (PTH)"])

    def test_programs_that_say_nothing_of_plating_keep_what_tells_them_apart(self) -> None:
        files = {"board-1-2.drl": DRILL, "board-2-3.drl": DRILL}
        self.assertEqual(_names(files), ["Drill (1-2)", "Drill (2-3)"])

    def test_the_whole_file_name_is_the_last_resort(self) -> None:
        self.assertEqual(
            _names({"a.drl": DRILL, "b.drl": DRILL}), ["Drill (a)", "Drill (b)"]
        )


class PackageFileTests(unittest.TestCase):
    def test_drill_maps_are_not_layers(self) -> None:
        files = package_files()
        drill_map = b"%TF.FileFunction,Drillmap*%\n" + gerber(SILK_MARK)
        files["drill-data/board-PTH-map.gbr"] = drill_map
        files["drill-data/board-NPTH-drl_map.gbr"] = gerber(SILK_MARK)
        package = FabricationPackage.from_files(files)
        self.assertEqual(
            [layer for layer in package.layers if "map" in layer.filename], []
        )
        # The map's legend would have widened the board.
        self.assertEqual(package.view()["size"], {"width": 20.0, "height": 10.0})

    def test_is_drill_map_reads_the_header_or_the_name(self) -> None:
        self.assertTrue(is_drill_map("x.gbr", b"%TF.FileFunction,Drillmap*%\n"))
        self.assertTrue(is_drill_map("board-PTH-drl_map.gbr", b""))
        self.assertFalse(is_drill_map("board-F_Cu.gbr", gerber(PAD)))

    def test_the_file_nearest_the_top_wins_a_name_clash(self) -> None:
        files = package_files()
        files["old/board-F_Silkscreen.gto"] = gerber(SILK_MARK)
        view = FabricationPackage.from_files(files).view()
        # The silkscreen mark at 30 mm would have widened the board.
        self.assertEqual(view["size"], {"width": 20.0, "height": 10.0})


class PackageViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.package = FabricationPackage.from_files(package_files())
        self.view = self.package.view()

    def test_layers_list_top_to_bottom_then_profile_drill_other(self) -> None:
        roles = [(layer["role"], layer["side"]) for layer in self.view["layers"]]
        self.assertEqual(
            roles,
            [
                ("silk", "top"),
                ("paste", "top"),
                ("mask", "top"),
                ("copper", "top"),
                ("copper", "bottom"),
                ("outline", "both"),
                ("drill", "both"),
                ("other", "top"),
            ],
        )

    def test_board_size_comes_from_the_profile(self) -> None:
        self.assertEqual(self.view["size"], {"width": 20.0, "height": 10.0})
        self.assertEqual(self.view["copperLayers"], 2)

    def test_board_frame_is_kicad_y_down(self) -> None:
        # A Gerber profile spanning Y 0..10 sits at -10..0 in board coordinates.
        x0, y0, x1, y1 = self.view["board"]
        self.assertEqual((x0, y0, x1, y1), (0.0, -10.0, 20.0, 0.0))

    def test_layer_ids_are_url_safe_and_unique(self) -> None:
        ids = [layer["id"] for layer in self.view["layers"]]
        self.assertEqual(len(ids), len(set(ids)))
        for layer_id in ids:
            self.assertRegex(layer_id, r"^[a-z0-9._-]+$")

    def test_every_layer_shares_the_board_viewbox(self) -> None:
        boxes = {
            layer["id"]: self.package.svg(layer["id"]).split('viewBox="')[1].split('"')[0]
            for layer in self.view["layers"]
        }
        self.assertEqual(len(set(boxes.values())), 1)

    def test_svg_for_an_unknown_layer_is_a_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self.package.svg("nope")

    def test_drill_tools_and_counts(self) -> None:
        drill = self.view["drill"]
        self.assertEqual(drill["holes"], 4)
        self.assertEqual(drill["slots"], 0)
        self.assertEqual(drill["smallest"], 0.3)
        tools = [(row["diameter"], row["plated"], row["hits"]) for row in drill["tools"]]
        # Plated first, then non-plated, each by size.
        self.assertEqual(tools, [(0.3, True, 2), (0.8, True, 1), (3.2, False, 1)])

    def test_a_broken_layer_does_not_hide_the_rest_of_the_package(self) -> None:
        files = package_files()
        files["board-F_Cu.gtl"] = b"not a gerber file\n"
        package = FabricationPackage.from_files(files)
        view = package.view()
        self.assertEqual(len(view["layers"]), 8)
        self.assertIn("f.cu", [layer["id"] for layer in view["layers"]])
        self.assertEqual(view["size"], {"width": 20.0, "height": 10.0})


class EmptyPackageTests(unittest.TestCase):
    def test_no_gerbers_is_an_error_not_an_empty_view(self) -> None:
        with self.assertRaises(FabricationViewError):
            FabricationPackage.from_files({"readme.txt": b"hello"})

    def test_a_drill_only_package_still_has_a_view(self) -> None:
        view = FabricationPackage.from_files({"board.drl": DRILL}).view()
        self.assertEqual(view["drill"]["holes"], 4)
        self.assertEqual(view["copperLayers"], 0)


class NearestExportTests(unittest.TestCase):
    def test_drills_in_a_subfolder_and_a_sibling_drill_folder_belong_to_the_export(self) -> None:
        from app.services.fabrication_view_service import nearest_export

        paths = ["Gerbers/b-F_Cu.gbr", "Gerbers/drill-data/b-PTH.drl", "Drill/b-NPTH.drl", "Gerbers/b-job.gbrjob"]
        self.assertEqual(nearest_export(paths), paths)

    def test_a_nested_or_sibling_export_is_left_out(self) -> None:
        from app.services.fabrication_view_service import nearest_export

        paths = [
            "Gerbers/b-F_Cu.gbr",
            "Gerbers/b.drl",
            "Gerbers/vendors/jlcpcb/CuTop.gbr",
            "Gerbers/vendors/jlcpcb/drill.drl",
            "archive/rev-a/b-F_Cu.gbr",
        ]
        self.assertEqual(nearest_export(paths), ["Gerbers/b-F_Cu.gbr", "Gerbers/b.drl"])

    def test_the_shallowest_export_wins_and_a_drill_map_is_not_an_export(self) -> None:
        from app.services.fabrication_view_service import nearest_export

        self.assertEqual(
            nearest_export(["b/x-F_Cu.gbr", "a/x-F_Cu.gbr", "maps/x-PTH-drl_map.gbr", "maps/x.drl"]),
            ["a/x-F_Cu.gbr", "maps/x.drl"],
        )

    def test_drills_alone_are_the_package(self) -> None:
        from app.services.fabrication_view_service import nearest_export

        self.assertEqual(nearest_export(["x-PTH.drl", "x-NPTH.drl"]), ["x-PTH.drl", "x-NPTH.drl"])


class FabricationResponseModelTests(unittest.TestCase):
    """The typed route answers exactly the dict ``FabricationPackage.view`` built."""

    def test_the_model_serializes_the_view_unchanged(self) -> None:
        from fastapi.encoders import jsonable_encoder

        from app.api import fabrication_view_models as models

        view = FabricationPackage.from_files(package_files()).view()
        typed = models.FabricationView.model_validate(view).model_dump(
            mode="json", by_alias=True, exclude_unset=True
        )
        self.assertEqual(typed, jsonable_encoder(view))


if __name__ == "__main__":
    unittest.main()
