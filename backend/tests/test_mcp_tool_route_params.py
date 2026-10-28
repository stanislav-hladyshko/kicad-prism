"""Every MCP tool sends path arguments that its route's template names.

A tool's ``args`` callable splits arguments into path parameters and query
parameters. A query argument placed in the path dict is silently dropped by the
route (the live ops check found ``get_slow_queries`` ignoring ``limit`` this
way), so each tool is called with every property filled in and its path dict
must name exactly the route's placeholders.
"""

from __future__ import annotations

import re
import unittest
from typing import Any

from app.services.mcp import registry
from app.services.mcp import tools as _tools  # noqa: F401  (registers every tool)


def _sample(schema: dict[str, Any]) -> Any:
    if "default" in schema:
        return schema["default"]
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), "string")
    if kind == "integer":
        return max(1, int(schema.get("minimum", 1)))
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return True
    if kind == "array":
        return []
    if kind == "object":
        return {}
    if schema.get("pattern") == r"^[0-9a-f]{40}$":
        return "0" * 40
    return "x"


class ToolRouteParameterTests(unittest.TestCase):
    def test_path_arguments_match_the_route_template(self) -> None:
        for tool in registry._TOOLS:
            with self.subTest(tool=tool.name):
                properties = tool.input_schema.get("properties") or {}
                arguments = {name: _sample(spec) for name, spec in properties.items()}
                path = tool.args(arguments)[0]
                placeholders = set(re.findall(r"{([^}]+)}", tool.route[1]))
                self.assertEqual(set(path), placeholders, tool.route[1])


if __name__ == "__main__":
    unittest.main()
