"""The one link builder: project URLs and ``prism://`` resource URIs (MCP-07)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.links import (  # noqa: E402
    RESOURCE_KINDS,
    ResourceRef,
    normalize_public_base_url,
    parse_resource_uri,
    project_url,
    resource_uri,
    view_url,
)

BASE = "https://prism.example.com"
SHA = "3f2c9a1b7e4d5c6a8b9f0e1d2c3b4a5968778695"


class ProjectUrlTests(unittest.TestCase):
    def test_parameter_order_is_the_call_order(self) -> None:
        self.assertEqual(
            project_url(BASE, "prj_1", commit=SHA, view="pcb", comment="c_1"),
            f"{BASE}/project/prj_1?commit={SHA}&view=pcb&comment=c_1",
        )
        self.assertEqual(
            project_url(BASE, "prj_1", section="history", base=SHA, compare=SHA),
            f"{BASE}/project/prj_1?section=history&base={SHA}&compare={SHA}",
        )

    def test_none_and_empty_parameters_are_dropped(self) -> None:
        self.assertEqual(
            project_url(BASE, "prj_1", commit=SHA, variant=None, item=""),
            f"{BASE}/project/prj_1?commit={SHA}",
        )
        self.assertEqual(project_url(BASE, "prj_1", variant=None), f"{BASE}/project/prj_1")

    def test_project_id_is_one_quoted_segment(self) -> None:
        self.assertEqual(
            project_url(BASE, "a/b c"),
            f"{BASE}/project/a%2Fb%20c",
        )

    def test_trailing_slash_on_the_base_is_dropped(self) -> None:
        self.assertEqual(project_url(f"{BASE}/", "prj_1"), f"{BASE}/project/prj_1")
        self.assertEqual(normalize_public_base_url(f"  {BASE}//  "), BASE)

    def test_empty_base_raises(self) -> None:
        for base in ("", "   ", "/"):
            with self.assertRaises(ValueError, msg=repr(base)):
                project_url(base, "prj_1")


class ResourceUriTests(unittest.TestCase):
    def test_round_trip_without_key(self) -> None:
        uri = resource_uri("prj_1", SHA, "component")
        self.assertEqual(uri, f"prism://project/prj_1/rev/{SHA}/component")
        self.assertEqual(
            parse_resource_uri(uri),
            ResourceRef("prj_1", SHA, "component", None),
        )

    def test_round_trip_with_key(self) -> None:
        uri = resource_uri("prj_1", SHA, "net", "MGMT.D0_P")
        self.assertEqual(uri, f"prism://project/prj_1/rev/{SHA}/net/MGMT.D0_P")
        self.assertEqual(
            parse_resource_uri(uri),
            ResourceRef("prj_1", SHA, "net", "MGMT.D0_P"),
        )

    def test_slashes_and_spaces_are_quoted(self) -> None:
        uri = resource_uri("a/b c", SHA, "sheet", "root/Sheet 1")
        self.assertEqual(
            uri,
            f"prism://project/a%2Fb%20c/rev/{SHA}/sheet/root%2FSheet%201",
        )
        self.assertEqual(
            parse_resource_uri(uri),
            ResourceRef("a/b c", SHA, "sheet", "root/Sheet 1"),
        )

    def test_uppercase_sha_is_lowercased(self) -> None:
        uri = resource_uri("prj_1", SHA.upper(), "bom")
        self.assertIn(f"/rev/{SHA}/", uri)
        self.assertEqual(parse_resource_uri(uri).commit, SHA)

    def test_every_kind_is_accepted(self) -> None:
        for kind in RESOURCE_KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(
                    parse_resource_uri(resource_uri("prj_1", SHA, kind)).kind,
                    kind,
                )

    def test_bad_uris_raise(self) -> None:
        for uri in (
            f"prism://project/prj_1/rev/{SHA[:12]}/component",  # short sha
            f"prism://project/prj_1/rev/{SHA}/widget",  # unknown kind
            f"https://prism.example.com/project/prj_1/rev/{SHA}/component",  # wrong scheme
            f"prism://project/prj_1/rev/{SHA}/component/U1/extra",  # extra segment
            f"prism://project/prj_1/rev/{SHA}/component/",  # empty key segment
            "prism://project/prj_1/rev",  # truncated
        ):
            with self.assertRaises(ValueError, msg=uri):
                parse_resource_uri(uri)

    def test_resource_uri_rejects_a_short_sha_and_an_unknown_kind(self) -> None:
        with self.assertRaises(ValueError):
            resource_uri("prj_1", SHA[:12], "component")
        with self.assertRaises(ValueError):
            resource_uri("prj_1", SHA, "widget")


class ViewUrlTests(unittest.TestCase):
    def test_parameters_are_written_in_the_fixed_order(self) -> None:
        self.assertEqual(
            view_url(
                BASE,
                "prj_1",
                tab="pcb",
                commit=SHA,
                sel=("U1",),
                nets=("GND",),
                page="/Sheet 1/",
                bbox=(1, 2, 3, 4),
                insets=True,
            ),
            f"{BASE}/project/prj_1?section=visualizers&tab=pcb&commit={SHA}"
            "&sel=U1&nets=GND&page=%2FSheet+1%2F&bbox=1,2,3,4&insets=1",
        )

    def test_empty_values_are_dropped(self) -> None:
        self.assertEqual(
            view_url(BASE, "prj_1", sel=(), nets=(), bbox=None, insets=False),
            f"{BASE}/project/prj_1?section=visualizers",
        )
        self.assertEqual(
            view_url(BASE, "prj_1", tab="", commit=None),
            f"{BASE}/project/prj_1?section=visualizers",
        )

    def test_list_items_are_quoted_one_at_a_time(self) -> None:
        url = view_url(
            BASE,
            "prj_1",
            sel=("U1", "R,2", "A/B", "C D", "板"),
            nets=("Power/+12V", "GND"),
        )
        self.assertIn("sel=U1,R%252C2,A%252FB,C%2520D,%25E6%259D%25BF&", url)
        self.assertIn("nets=Power%252F%252B12V,GND", url)
        self.assertIn("sel=U1", view_url(BASE, "prj_1", sel="U1"))

    def test_lists_read_back_the_way_the_frontend_reads_them(self) -> None:
        # lib/view-state-url.ts: URLSearchParams.get (one decode), split on
        # commas, decodeURIComponent each item.
        from urllib.parse import parse_qs, unquote, urlsplit

        items = ["U1", "R,2", "A/B", "C D", "板", "50%"]
        query = parse_qs(urlsplit(view_url(BASE, "prj_1", sel=items, nets=items)).query)
        for key in ("sel", "nets"):
            self.assertEqual([unquote(part) for part in query[key][0].split(",")], items)

    def test_bbox_numbers_have_no_trailing_zeros(self) -> None:
        self.assertIn(
            "bbox=1,2.5,3.125,-0.5",
            view_url(BASE, "prj_1", bbox=(1.0, 2.50, 3.125, -0.5)),
        )
        self.assertIn(
            "bbox=0,0,12,8",
            view_url(BASE, "prj_1", bbox=(0.0, -0.0, 12.000, 8.0)),
        )

    def test_the_project_id_stays_one_path_segment(self) -> None:
        self.assertEqual(
            view_url(BASE, "a/b c", tab="sch"),
            f"{BASE}/project/a%2Fb%20c?section=visualizers&tab=sch",
        )

    def test_empty_base_raises(self) -> None:
        with self.assertRaises(ValueError):
            view_url("  ", "prj_1")


if __name__ == "__main__":
    unittest.main()
