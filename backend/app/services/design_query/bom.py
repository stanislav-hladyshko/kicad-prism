"""The engineering BOM, computed server-side (ADR scripting-and-mcp §3.8).

This is the port of `frontend/src/lib/engineering-bom.ts`: a BOM line is one
value, carrying its references, quantity and the fields every component in the
group agrees on. The UI renders this model and the API serves it, so an agent
and a person looking at the same revision see the same lines.

`fixtures/bom/` is the shared fixture. `test_project_bom.py` holds both sides
to it; a change here must keep the TypeScript and Python answers equal.

Natural order is JavaScript's ``localeCompare(..., {numeric: true,
sensitivity: "base"})``: numeric runs compare as numbers (R2 before R10) and
text compares case- and accent-insensitively.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Mapping

BOM_REQUIRED_COLUMNS = (
    "Reference",
    "Qty",
    "Value",
    "DNP",
    "In BOM",
    "Description",
    "Datasheet",
    "Manufacturer",
    "Manufacturer Part Number",
    "Vendor",
    "Vendor Part Number",
    "Footprint",
    "Mass (g)",
    "RQjC (C/W)",
    "RQjC_top (C/W)",
    "Temp_max (C)",
    "Temp_min (C)",
    "Power Dissipation (W)",
    "Rate",
)

#: One or more digits, the run that compares as a number.
_DIGITS = re.compile(r"(\d+)")


def _fold(text: str) -> str:
    """Base sensitivity: case-insensitive, accent-insensitive."""

    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(character for character in decomposed if not unicodedata.combining(character)).casefold()


def _natural_key(text: str) -> tuple[tuple[int, int, str], ...]:
    """Chunks that sort text as ``localeCompare`` with ``numeric: true`` does.

    A chunk is ``(0, number, "")`` for a digit run and ``(1, 0, folded text)``
    otherwise, so a number compares with a number and text with text.
    """

    key: list[tuple[int, int, str]] = []
    for index, chunk in enumerate(_DIGITS.split(text)):
        if index % 2:
            key.append((0, int(chunk), ""))
        else:
            key.append((1, 0, _fold(chunk)))
    return tuple(key)


def _field_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _fields(component: Mapping[str, Any]) -> Mapping[str, Any]:
    fields = component.get("fields")
    return fields if isinstance(fields, Mapping) else {}


def component_fields(component: Mapping[str, Any]) -> dict[str, str]:
    """A component's top-level value and footprint, then its own fields."""

    result = {
        "Value": component.get("value") or "",
        "Footprint": component.get("footprint") or "",
    }
    for key, value in _fields(component).items():
        result[str(key)] = _field_text(value)
    return result


def is_internal_field(field: str) -> bool:
    normalized = field.casefold()
    return normalized.startswith("_") or normalized.startswith("kicad_")


def common_fields(components: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """The field values every component in the group agrees on, else ``Mixed``."""

    rows = [component_fields(component) for component in components]
    keys: dict[str, None] = {}
    for row in rows:
        for key in row:
            keys.setdefault(key, None)
    result: dict[str, str] = {}
    for key in keys:
        values: list[str] = []
        for row in rows:
            value = row.get(key) or ""
            if value not in values:
                values.append(value)
        result[key] = values[0] if len(values) == 1 else "Mixed"
    return result


def bom_columns(components: Iterable[Mapping[str, Any]]) -> list[str]:
    """Required columns, then the components' own fields in name order."""

    required = set(BOM_REQUIRED_COLUMNS)
    extras: set[str] = set()
    for component in components:
        for field in _fields(component):
            field = str(field)
            if field not in required and not is_internal_field(field):
                extras.add(field)
    return [*BOM_REQUIRED_COLUMNS, *sorted(extras, key=_fold)]


def _flag_text(value: Any) -> str:
    return _field_text(value)


def is_dnp(component: Mapping[str, Any]) -> bool:
    return _flag_text(_fields(component).get("DNP")).casefold() == "yes"


def is_in_bom(component: Mapping[str, Any]) -> bool:
    return _flag_text(_fields(component).get("In BOM")).casefold() != "no"


def is_assembled(component: Mapping[str, Any]) -> bool:
    return is_in_bom(component) and not is_dnp(component)


def filter_components(
    components: Iterable[Mapping[str, Any]], assembly: str
) -> list[Mapping[str, Any]]:
    if assembly not in ("all", "assembly"):
        raise ValueError("assembly must be 'all' or 'assembly'")
    if assembly == "all":
        return list(components)
    return [component for component in components if is_assembled(component)]


def build_bom(components: list[dict[str, Any]], assembly: str) -> dict[str, Any]:
    """The BOM for ``components`` after the ``assembly`` filter.

    ``components`` are the semantic index's ``components`` records. The answer
    is ``{"columns": [...], "lines": [{key, quantity, references, fields}]}``,
    the same shape as the frontend's ``buildBom``.
    """

    all_components = list(components)
    filtered = filter_components(all_components, assembly)
    by_value: dict[str, list[Mapping[str, Any]]] = {}
    for component in filtered:
        fields = component_fields(component)
        value_key = fields["Value"].strip().lower()
        by_value.setdefault(value_key, []).append(component)

    lines: list[dict[str, Any]] = []
    for key, group in by_value.items():
        sorted_group = sorted(group, key=lambda component: _natural_key(str(component.get("reference") or "")))
        lines.append(
            {
                "key": f"value:{key}",
                "quantity": len(sorted_group),
                "references": [str(component.get("reference") or "") for component in sorted_group],
                "fields": common_fields(sorted_group),
            }
        )
    lines.sort(key=lambda line: _natural_key(line["fields"].get("Value") or ""))
    return {"columns": bom_columns(all_components), "lines": lines}
