"""Design queries over a revision's semantic index (ADR §3.8).

The fixture is a small ``prism.semantic_index.a2`` member: two sheets, eight
components and six nets, one of them with ``/`` in its name. The tests pin the
three answers (search ranking, one component, one net) and the parsed-index
cache.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.services import semantic_index_bundle
from app.services.design_query import index as design_index

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "design_query" / "semantic_index.json"
REVISION_KEY = "a" * 64
GENERATOR = "g" * 64


def _index() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class FindTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = _index()

    def test_a_reference_exact_match_comes_first(self) -> None:
        hits = design_index.find(self.index, "u1", {"component"})
        self.assertEqual(hits[0]["reference"], "U1")
        self.assertEqual(hits[0]["matched"], "reference")

    def test_a_reference_prefix_matches_case_insensitively(self) -> None:
        index = {
            "components": [
                {"reference": "R1", "value": "10k", "fields": {}},
                {"reference": "R2", "value": "1k", "fields": {}},
            ],
            "nets": [],
        }
        hits = design_index.find(index, "r", {"component"})
        self.assertEqual([hit["reference"] for hit in hits], ["R1", "R2"])

    def test_natural_reference_order_puts_r2_before_r10(self) -> None:
        index = {
            "components": [
                {"reference": "R10", "value": "1k", "fields": {}},
                {"reference": "R2", "value": "1k", "fields": {}},
            ],
            "nets": [],
        }
        hits = design_index.find(index, "r", {"component"})
        self.assertEqual([hit["reference"] for hit in hits], ["R2", "R10"])

    def test_references_rank_ahead_of_field_matches(self) -> None:
        # Found by a real MCP client: "R" listed every part with an "r" in its
        # description ahead of the resistors.
        index = {
            "components": [
                {"reference": "C1", "value": "10uF", "fields": {"Description": "Ceramic capacitor"}},
                {"reference": "J1", "value": "USB-C Receptacle", "fields": {}},
                {"reference": "R2", "value": "1k", "fields": {}},
                {"reference": "R1", "value": "10k", "fields": {}},
            ],
            "nets": [{"name": "VBUS_R", "pins": []}],
        }
        hits = design_index.find(index, "r", {"component", "net"})
        self.assertEqual(
            [hit["reference"] or hit["name"] for hit in hits],
            ["R1", "R2", "J1", "VBUS_R", "C1"],
        )

    def test_a_value_matches_by_substring(self) -> None:
        hits = design_index.find(self.index, "lm317", {"component"})
        self.assertEqual([hit["reference"] for hit in hits], ["U1"])
        self.assertEqual(hits[0]["matched"], "value")

    def test_a_field_value_matches_and_names_the_field(self) -> None:
        hits = design_index.find(self.index, "rc0603fr-0710kl", {"component"})
        self.assertEqual([hit["reference"] for hit in hits], ["R1"])
        self.assertEqual(hits[0]["matched"], "field")
        self.assertEqual(hits[0]["field"], "Manufacturer Part Number")

    def test_a_net_matches_by_name_substring(self) -> None:
        hits = design_index.find(self.index, "power", {"net"})
        self.assertEqual([hit["name"] for hit in hits], ["Power/+12V"])
        self.assertEqual(hits[0]["kind"], "net")

    def test_a_net_name_with_a_slash_is_searchable(self) -> None:
        hits = design_index.find(self.index, "u1/ad", {"net"})
        self.assertEqual([hit["name"] for hit in hits], ["U1/ADJ"])

    def test_a_mixed_query_puts_the_exact_component_first(self) -> None:
        hits = design_index.find(self.index, "u1", {"component", "net"})
        self.assertEqual([(hit["kind"], hit["reference"] or hit["name"]) for hit in hits],
                         [("component", "U1"), ("net", "U1/ADJ")])

    def test_kinds_filter_the_answer(self) -> None:
        hits = design_index.find(self.index, "u1", {"net"})
        self.assertEqual([hit["kind"] for hit in hits], ["net"])

    def test_limit_bounds_the_answer(self) -> None:
        hits = design_index.find(self.index, "u1", {"component"}, limit=1)
        self.assertEqual([hit["reference"] for hit in hits], ["U1"])

    def test_an_empty_query_returns_nothing(self) -> None:
        self.assertEqual(design_index.find(self.index, "  ", {"component", "net"}), [])


class ComponentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = _index()

    def test_a_component_carries_fields_footprint_sheet_and_pins(self) -> None:
        found = design_index.component(self.index, "U1")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found["reference"], "U1")
        self.assertEqual(found["value"], "LM317T")
        self.assertEqual(found["footprint"], "Package_TO_SOT_THT:TO-220-3_Vertical")
        self.assertEqual(found["libraryRef"], "Regulator_Linear:LM317_TO220")
        self.assertEqual(found["sheet"], "/Power/")
        self.assertEqual(found["page"], "power.kicad_sch")
        self.assertEqual(found["fields"]["Manufacturer Part Number"], "LM317T")
        self.assertFalse(found["dnp"])
        self.assertTrue(found["inBom"])
        self.assertEqual(
            [(pin["pin"], pin["pinName"], pin["net"]) for pin in found["pins"]],
            [("1", "IN", "+12V"), ("2", "ADJ", "U1/ADJ"), ("3", "OUT", "VOUT")],
        )

    def test_dnp_comes_from_the_default_assembly(self) -> None:
        found = design_index.component(self.index, "D1")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertTrue(found["dnp"])
        self.assertEqual(found["assembly"], {"dnp": True})

    def test_other_assembly_flags_are_reported(self) -> None:
        found = design_index.component(self.index, "R2")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found["assembly"], {"excludeFromPosFiles": True})
        self.assertFalse(found["dnp"])

    def test_a_reference_is_case_insensitive(self) -> None:
        self.assertEqual(design_index.component(self.index, "u1")["reference"], "U1")

    def test_an_unknown_reference_is_none(self) -> None:
        self.assertIsNone(design_index.component(self.index, "X99"))


class NetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = _index()

    def test_a_net_name_with_a_slash_resolves(self) -> None:
        found = design_index.net(self.index, "Power/+12V")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found["name"], "Power/+12V")
        self.assertEqual(found["netClass"], "Power")
        self.assertEqual(found["components"], ["J3"])
        self.assertEqual(found["pins"], [{"reference": "J3", "pin": "1", "pinName": "Pin_1"}])

    def test_a_net_lists_its_pins_and_components_in_natural_order(self) -> None:
        found = design_index.net(self.index, "GND")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found["components"], ["C1", "C2", "D1", "J3", "R2", "U2"])
        self.assertEqual(len(found["pins"]), 7)
        self.assertEqual(found["pins"][0], {"reference": "C1", "pin": "2", "pinName": "~"})

    def test_an_absorbed_alias_resolves_to_its_net(self) -> None:
        found = design_index.net(self.index, "VIN")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found["name"], "+12V")
        self.assertEqual(found["aliases"], ["VIN"])

    def test_an_unknown_net_is_none(self) -> None:
        self.assertIsNone(design_index.net(self.index, "NOPE"))


class LoadTests(unittest.TestCase):
    def setUp(self) -> None:
        design_index.clear_cache()
        self.addCleanup(design_index.clear_cache)
        self.project = SimpleNamespace(id="p1", path="/repo/project")

    def test_load_returns_the_design_key_and_reuses_the_parsed_index(self) -> None:
        artifact = semantic_index_bundle.ArtifactFile(FIXTURE, REVISION_KEY, GENERATOR)
        with mock.patch.object(
            design_index.semantic_index_service, "artifact_file", return_value=artifact
        ), mock.patch.object(design_index.json, "load", wraps=json.load) as parse:
            first, design_key = design_index.load(self.project, "commit")
            second, again = design_index.load(self.project, "commit")
        self.assertEqual(design_key, REVISION_KEY)
        self.assertEqual(again, REVISION_KEY)
        self.assertEqual(first["schema"], "prism.semantic_index.a2")
        self.assertIs(second, first)
        self.assertEqual(parse.call_count, 1)

    def test_the_cache_keeps_two_revisions(self) -> None:
        commits = [f"{number:040x}" for number in range(3)]
        artifacts = {
            commit: semantic_index_bundle.ArtifactFile(FIXTURE, commit, GENERATOR)
            for commit in commits
        }

        def artifact_file(_project: object, commit: str) -> semantic_index_bundle.ArtifactFile:
            return artifacts[commit]

        with mock.patch.object(
            design_index.semantic_index_service, "artifact_file", side_effect=artifact_file
        ), mock.patch.object(design_index.json, "load", wraps=json.load) as parse:
            for commit in commits:
                design_index.load(self.project, commit)
            self.assertEqual(design_index.cache_stats()["entries"], 2)
            self.assertEqual(parse.call_count, 3)
            design_index.load(self.project, commits[0])  # evicted by the third
            self.assertEqual(parse.call_count, 4)
            design_index.load(self.project, commits[0])  # now cached
            self.assertEqual(parse.call_count, 4)


if __name__ == "__main__":
    unittest.main()
