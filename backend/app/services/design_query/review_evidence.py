"""What one Design Comparison change is, as review evidence (ADR §3.8).

The evidence layer of the server-side review queue. It is the port of the
frontend's `comparison-change-facts.ts`, `comparison-property-model.ts` and
`comparison-review-noise.ts`: the vocabulary a change is described in, its
authored property deltas, the BOM part identity a component groups by, and the
pass that turns parser events into reviewer-level evidence.

`review_groups.py` owns the rows these facts produce. Nothing here knows about
groups or the queue.
"""

from __future__ import annotations

import json
import re
from functools import cmp_to_key
from typing import Any, Iterable, Mapping, Optional, Sequence

# ---------------------------------------------------------------------------
# Change kind table and categories
# ---------------------------------------------------------------------------

#: ``CATEGORY_META``'s display order from ``@/lib/diff-grouping``.
CATEGORY_ORDER: dict[str, int] = {
    "rules": 0,
    "components": 1,
    "symbols": 1,
    "nets": 2,
    "zones": 3,
    "sheets": 4,
    "text": 5,
    "graphics": 6,
    "other": 9,
}

#: Native object kind, its reviewer noun and the traits the review modules ask
#: about. Mirrors ``CHANGE_KINDS`` in ``comparison-change-kinds.ts``.
_CHANGE_KINDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "symbol": ("symbol", ()),
    "pin": ("pin", ("schematic-exact",)),
    "sheet": ("sheet", ("schematic-exact",)),
    "sheet-pin": ("sheet pin", ("schematic-exact",)),
    "no-connect": ("no connect", ("schematic-exact",)),
    "wire": ("wire", ("wiring", "schematic-net")),
    "junction": ("junction", ("wiring", "schematic-net")),
    "bus": ("bus", ("wiring", "schematic-exact")),
    "bus-entry": ("bus entry", ("wiring", "schematic-exact")),
    "label": ("label", ("schematic-net", "schematic-label")),
    "footprint": ("footprint", ("fabrication",)),
    "pad": ("pad", ("component-child", "fabrication")),
    "track": ("track", ("copper", "fabrication")),
    "arc": ("arc", ("copper", "fabrication")),
    "via": ("via", ("copper", "via-span", "fabrication")),
    "zone": ("zone", ("component-child", "fabrication")),
    "footprint-graphic": (
        "footprint graphic",
        ("component-child", "footprint-primitive", "pcb-graphic"),
    ),
    "footprint-text": (
        "footprint text",
        ("component-child", "footprint-primitive", "documentation"),
    ),
    "graphic": ("graphic", ("pcb-graphic",)),
    "group": ("group", ()),
    "text": ("text", ("documentation",)),
    "image": ("image", ("documentation",)),
    "table": ("table", ("documentation",)),
    "board-constraint": ("board constraint", ("rule",)),
    "board-default": ("board default", ("rule",)),
    "custom-rule": ("custom rule", ("rule",)),
    "drc-exclusion": ("drc exclusion", ("rule",)),
    "drc-severity": ("drc severity", ("rule",)),
    "erc-exclusion": ("erc exclusion", ("rule",)),
    "erc-pin-rule": ("erc pin rule", ("rule",)),
    "fabrication-output": ("fabrication output", ("rule",)),
    "net-class": ("net class", ("rule",)),
    "net-class-assignment": ("net class assignment", ("rule",)),
    "routing-preset": ("routing preset", ("rule",)),
    "teardrop-setting": ("teardrop setting", ("rule",)),
    "zone-setting": ("zone setting", ("rule",)),
}


def _kinds_with(trait: str) -> frozenset[str]:
    return frozenset(
        kind for kind, (_, traits) in _CHANGE_KINDS.items() if trait in traits
    )


SCHEMATIC_WIRING_KINDS = _kinds_with("wiring")
PCB_COPPER_KINDS = _kinds_with("copper")
PCB_COMPONENT_CHILD_KINDS = _kinds_with("component-child")
RULE_KINDS = _kinds_with("rule")
SCHEMATIC_LABEL_KINDS = _kinds_with("schematic-label")
FOOTPRINT_PRIMITIVE_KINDS = _kinds_with("footprint-primitive")

NET_REASON_CODES = frozenset(
    ("connectivity-changed", "net-renamed", "label-count-changed")
)
SCHEMATIC_LAYOUT_REASONS = frozenset(
    ("moved", "rotated", "mirrored", "properties-changed")
)
LAYOUT_FIELD_NAMES = frozenset(("position", "rotation", "mirror"))
STRUCTURED_GEOMETRY_KEYS = frozenset(
    (
        "at",
        "points",
        "position",
        "rotation",
        "size",
        "uuid",
        "tstamp",
        "sourceUuid",
        "busUid",
    )
)

#: ICU's primary order for printable ASCII, lowercased: punctuation, then
#: digits, then letters. JavaScript's default ``localeCompare`` sorts by this
#: order and only then by case, so ``"#PWR01"`` follows ``"/Power/3V3"`` and
#: precedes ``"bus"``. Comparing Python code points instead would put every
#: uppercase letter before every lowercase one and reorder review rows.
_ICU_ASCII_ORDER = (
    " _-,;:!?.'\"()[]{}@*/\\&#%`^+<=>|~$"
    "0123456789"
    "abcdefghijklmnopqrstuvwxyz"
)
_ICU_ASCII_RANK = {char: index for index, char in enumerate(_ICU_ASCII_ORDER)}

#: The BOM's natural order, mirrored from ``design_query.bom``.
_DIGITS = re.compile(r"(\d+)")


# ---------------------------------------------------------------------------
# Small value helpers
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    """``String(value ?? "")``, without turning ``None`` into ``"None"``."""

    if value is None:
        return ""
    return str(value)


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _js_string(value: Any) -> str:
    """A JavaScript ``String(value)`` for the scalar types a review carries."""

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _fold(text: str) -> str:
    return text.casefold()


def _natural_key(text: str) -> tuple[tuple[int, int, str], ...]:
    """The key ``localeCompare(..., {numeric: true, sensitivity: "base"})`` sorts by."""

    key: list[tuple[int, int, str]] = []
    for index, chunk in enumerate(_DIGITS.split(text)):
        if index % 2:
            key.append((0, int(chunk), ""))
        else:
            key.append((1, 0, _fold(chunk)))
    return tuple(key)


def _collation_key(text: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """ICU's primary (case-folded) order, then lowercase-before-uppercase."""

    primary = tuple(_ICU_ASCII_RANK.get(char.casefold(), 1000 + ord(char)) for char in text)
    case = tuple(1 if char.isupper() else 0 for char in text)
    return primary, case


def _locale_compare(left: str, right: str) -> int:
    left_key, right_key = _collation_key(left), _collation_key(right)
    if left_key != right_key:
        return -1 if left_key < right_key else 1
    if left == right:
        return 0
    return -1 if left < right else 1


def _normalized_value(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return [_normalized_value(entry) for entry in value]
    if isinstance(value, Mapping):
        return {
            key: _normalized_value(value[key])
            for key in sorted(value.keys(), key=str)
        }
    return value


def _encoded_value(value: Any) -> str:
    return json.dumps(
        _normalized_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _values_equivalent(old_value: Any, new_value: Any) -> bool:
    return _encoded_value(old_value) == _encoded_value(new_value)


# ---------------------------------------------------------------------------
# What a change is (comparison-change-facts.ts)
# ---------------------------------------------------------------------------


def object_kind(change: Mapping[str, Any]) -> str:
    value = change.get("objectKind")
    if value is None:
        value = "object"
    return _text(value).lower()


def union_layers(change: Mapping[str, Any]) -> list[str]:
    layers = set((change.get("base") or {}).get("layers") or [])
    layers.update((change.get("head") or {}).get("layers") or [])
    return sorted(layers)


def change_layers(change: Mapping[str, Any]) -> list[str]:
    layers: list[str] = list(union_layers(change))
    layers.extend(
        layer
        for layer in (
            (change.get("base") or {}).get("layer"),
            (change.get("head") or {}).get("layer"),
        )
        if layer
    )
    return layers


def has_native_parent(change: Mapping[str, Any]) -> bool:
    return bool(
        (change.get("base") or {}).get("parentSourceId")
        or (change.get("head") or {}).get("parentSourceId")
    )


def _normalize_category(category: str) -> str:
    if category == "board":
        return "other"
    return category if category in CATEGORY_ORDER else "other"


def _is_schematic_power_reference(change: Mapping[str, Any]) -> bool:
    value = _first_not_none(change.get("reference"), change.get("label"))
    return change.get("domain") == "schematic" and bool(
        re.match(r"^#(?:PWR|FLG)", _text(value), re.IGNORECASE)
    )


def semantic_category(change: Mapping[str, Any]) -> str:
    category = _normalize_category(_text(change.get("category")))
    kind = object_kind(change)
    if category == "rules" or kind in RULE_KINDS:
        return "rules"
    if change.get("domain") == "pcb" and change.get("reference"):
        if kind == "pad":
            reasons = change.get("reasons") or []
            return (
                "nets"
                if change.get("kind") == "changed"
                and ("net-changed" in reasons or "Net" in (change.get("fields") or {}))
                else "components"
            )
        if kind in PCB_COMPONENT_CHILD_KINDS and (kind != "zone" or has_native_parent(change)):
            return "components"
    if category == "zones":
        return category
    reasons = change.get("reasons") or []
    if (
        _is_schematic_power_reference(change)
        or change.get("net")
        or any(reason in NET_REASON_CODES for reason in reasons)
    ):
        return "nets"
    return category


def reference_for(change: Mapping[str, Any]) -> Optional[str]:
    head = change.get("head") or {}
    base = change.get("base") or {}
    targets = (change.get("details") or {}).get("targets") or []
    target_reference = next(
        (target.get("reference") for target in targets if target.get("reference")),
        None,
    )
    return _first_not_none(
        change.get("reference"),
        head.get("reference"),
        base.get("reference"),
        target_reference,
    )


def page_for(change: Mapping[str, Any]) -> str:
    head = change.get("head") or {}
    base = change.get("base") or {}
    targets = (change.get("details") or {}).get("targets") or []
    target_path = next(
        (target.get("documentPath") for target in targets if target.get("documentPath")),
        None,
    )
    return (
        change.get("page")
        or head.get("documentPath")
        or base.get("documentPath")
        or target_path
        or "Schematic"
    )


def field_sides(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, Mapping):
        return None
    if "old" not in value and "new" not in value:
        return None
    return {"old": value.get("old"), "new": value.get("new")}


def _is_layout_field(name: str) -> bool:
    folded = name.strip().lower()
    return folded in LAYOUT_FIELD_NAMES or folded.endswith(" attributes")


def _parsed_structured_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _without_structured_geometry(value: Any) -> Any:
    parsed = _parsed_structured_value(value)
    if isinstance(parsed, list):
        return [_without_structured_geometry(entry) for entry in parsed]
    if isinstance(parsed, Mapping):
        return {
            key: _without_structured_geometry(entry)
            for key, entry in parsed.items()
            if key not in STRUCTURED_GEOMETRY_KEYS
        }
    return parsed


def _is_structured_geometry_only_field(
    name: str, sides: Mapping[str, Any]
) -> bool:
    if not name.strip().lower().endswith("content"):
        return False
    old_value = _parsed_structured_value(sides.get("old"))
    new_value = _parsed_structured_value(sides.get("new"))
    if not isinstance(old_value, Mapping) or not isinstance(new_value, Mapping):
        return False
    return not _values_equivalent(old_value, new_value) and _values_equivalent(
        _without_structured_geometry(old_value),
        _without_structured_geometry(new_value),
    )


def has_structured_geometry_only_field(change: Mapping[str, Any]) -> bool:
    for name, value in (change.get("fields") or {}).items():
        sides = field_sides(value)
        if sides and _is_structured_geometry_only_field(name, sides):
            return True
    return False


def meaningful_field_entries(
    change: Mapping[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    entries: list[tuple[str, dict[str, Any]]] = []
    for name, value in (change.get("fields") or {}).items():
        if _is_layout_field(name):
            continue
        sides = field_sides(value)
        if not sides or _values_equivalent(sides.get("old"), sides.get("new")):
            continue
        if _is_structured_geometry_only_field(name, sides):
            continue
        entries.append((name, sides))
    return entries


def is_same_page_layout_only(change: Mapping[str, Any]) -> bool:
    if change.get("domain") != "schematic" or change.get("kind") != "changed":
        return False
    if (
        has_structured_geometry_only_field(change)
        and not meaningful_field_entries(change)
    ):
        return True
    reasons = change.get("reasons") or []
    if not reasons or any(reason not in SCHEMATIC_LAYOUT_REASONS for reason in reasons):
        return False
    return not meaningful_field_entries(change)


def parent_symbol_key(change: Mapping[str, Any]) -> str:
    return f"{change.get('kind')}:{reference_for(change) or ''}:{page_for(change)}"


def string_field_pair(
    change: Mapping[str, Any], names: Sequence[str]
) -> Optional[dict[str, str]]:
    for name in names:
        sides = field_sides((change.get("fields") or {}).get(name))
        if sides and isinstance(sides.get("old"), str) and isinstance(sides.get("new"), str):
            return {"old": sides["old"], "new": sides["new"]}
    return None


def compact_value(value: Any) -> str:
    normalized = _normalized_value(value)
    if normalized is None or normalized == "":
        return "—"
    if isinstance(normalized, (str, int, float, bool)):
        return _js_string(normalized)
    encoded = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return f"{encoded[:39]}…" if len(encoded) > 42 else encoded


def _humanize(value: str) -> str:
    spaced = re.sub(r"[_-]+", " ", value)
    return re.sub(r"\b\w", lambda match: match.group(0).upper(), spaced)


# ---------------------------------------------------------------------------
# Reviewer-facing verbs and property deltas (comparison-property-model.ts)
# ---------------------------------------------------------------------------

REASON_VERB: dict[str, str] = {
    "renamed": "Re-annotated",
    "lib-changed": "Replaced",
    "instance-replaced": "Replaced",
    "symbol-fields-changed": "Updated",
    "properties-changed": "Updated",
    "dnp-changed": "Updated",
    "content-changed": "Updated",
    "object-added": "Added",
    "object-removed": "Removed",
    "instance-count-changed": "Re-instanced",
    "moved": "Moved",
    "rotated": "Rotated",
    "mirrored": "Mirrored",
    "layer-changed": "Re-layered",
    "net-changed": "Re-netted",
    "net-renamed": "Renamed",
    "connectivity-changed": "Reconnected",
    "label-count-changed": "Re-labelled",
    "bus-membership-changed": "Re-grouped",
    "sheet-changed": "Re-pathed",
    "re-pathed": "Re-pathed",
}

_DEFAULT_VERB = "Updated"

_FIELD_REASON_AFFINITY: tuple[
    tuple[re.Pattern[str], tuple[str, ...], Optional[str]], ...
] = (
    (re.compile(r"^(designator|reference|refdes)$", re.IGNORECASE), ("renamed",), "Re-annotated"),
    (
        re.compile(
            r"^(lib_?id|library|design item id|symbol|footprint library)$",
            re.IGNORECASE,
        ),
        ("lib-changed", "instance-replaced"),
        None,
    ),
    (re.compile(r"^position$", re.IGNORECASE), ("moved",), "Moved"),
    (re.compile(r"^rotation$", re.IGNORECASE), ("rotated",), "Rotated"),
    (re.compile(r"^mirror$", re.IGNORECASE), ("mirrored",), "Mirrored"),
    (re.compile(r"^layers?$", re.IGNORECASE), ("layer-changed",), "Re-layered"),
    (re.compile(r"^net$", re.IGNORECASE), ("net-changed", "net-renamed"), "Re-netted"),
    (re.compile(r"^dnp$", re.IGNORECASE), ("dnp-changed",), "Updated"),
    (
        re.compile(r"^sheet$", re.IGNORECASE),
        ("sheet-changed", "re-pathed"),
        "Re-pathed",
    ),
)

_FIELD_EDIT_REASONS = ("symbol-fields-changed", "properties-changed", "content-changed")

_ROUTING_CONTEXT_FIELDS = frozenset(("position", "reference"))

_FOOTPRINT_PRIMITIVE_FIELDS = frozenset(
    ("graphic type", "layer", "position", "reference", "rotation", "stroke")
)

NM_PER_MM = 1_000_000


def _verb_for_property(label: str, reasons: Iterable[str]) -> str:
    declared = set(reasons)
    folded = label.strip()
    for pattern, candidates, fallback in _FIELD_REASON_AFFINITY:
        if not pattern.match(folded):
            continue
        matched = next((reason for reason in candidates if reason in declared), None)
        if matched:
            return REASON_VERB.get(matched, _DEFAULT_VERB)
        if fallback:
            return fallback
        break
    field_edit = next((reason for reason in _FIELD_EDIT_REASONS if reason in declared), None)
    if field_edit:
        return REASON_VERB.get(field_edit, _DEFAULT_VERB)
    for reason in REASON_VERB:
        if reason in declared:
            return REASON_VERB[reason]
    return _DEFAULT_VERB


def format_value(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(format_value(entry) for entry in value)
    if isinstance(value, Mapping):
        return "; ".join(
            f"{_humanize(str(key))}: {format_value(entry)}"
            for key, entry in value.items()
        )
    return _js_string(value)


def _field_pair(value: Any) -> dict[str, Any]:
    sides = value if isinstance(value, Mapping) else {}
    return {"oldValue": sides.get("old"), "newValue": sides.get("new")}


def _point_mm(point: Mapping[str, Any]) -> tuple[float, float]:
    return (
        float(point.get("xNm") or 0) / NM_PER_MM,
        float(point.get("yNm") or 0) / NM_PER_MM,
    )


def _position_text(value: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not value:
        return None
    x, y = _point_mm(value)
    return f"{_js_string(x)}, {_js_string(y)}"


def _without_schematic_placement(value: Any) -> Any:
    if isinstance(value, list):
        return [_without_schematic_placement(entry) for entry in value]
    if isinstance(value, Mapping):
        return {
            key: _without_schematic_placement(entry)
            for key, entry in value.items()
            if str(key).lower() not in ("at", "position")
        }
    return value


def _schematic_placement_only_field(label: str, value: Any) -> bool:
    folded = label.strip().lower()
    if folded == "position":
        return True
    if not folded.endswith("attributes"):
        return False
    pair = _field_pair(value)
    return _encoded_value(_without_schematic_placement(pair["oldValue"])) == _encoded_value(
        _without_schematic_placement(pair["newValue"])
    )


def _is_routing(change: Mapping[str, Any]) -> bool:
    return (change.get("objectKind") or "") in PCB_COPPER_KINDS


def property_deltas(changes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    routing_group = len(changes) > 1 and all(_is_routing(change) for change in changes)
    aggregate: dict[str, dict[str, Any]] = {}
    reasons = {reason for change in changes for reason in (change.get("reasons") or [])}

    def push(row: Mapping[str, Any]) -> None:
        key = f"{row['label']}:{format_value(row['oldValue'])}:{format_value(row['newValue'])}"
        if key in seen:
            return
        seen.add(key)
        rows.append({**row, "verb": _verb_for_property(str(row["label"]), reasons)})

    for change in changes:
        for label, value in (change.get("fields") or {}).items():
            folded_label = str(label).lower()
            if (
                change.get("domain") == "schematic"
                and change.get("classification") != "secondary"
                and _schematic_placement_only_field(str(label), value)
            ):
                continue
            if routing_group:
                if folded_label in _ROUTING_CONTEXT_FIELDS:
                    continue
                pair = _field_pair(value)
                entry = aggregate.get(folded_label) or {
                    "label": label,
                    "oldValues": {},
                    "newValues": {},
                }
                if pair["oldValue"] is not None:
                    entry["oldValues"][format_value(pair["oldValue"])] = pair["oldValue"]
                if pair["newValue"] is not None:
                    entry["newValues"][format_value(pair["newValue"])] = pair["newValue"]
                aggregate[folded_label] = entry
                continue
            if (
                len(changes) > 1
                and object_kind(change) in FOOTPRINT_PRIMITIVE_KINDS
                and folded_label in _FOOTPRINT_PRIMITIVE_FIELDS
            ):
                continue
            pair = _field_pair(value)
            push(
                {
                    "id": f"{change.get('id')}:field:{label}",
                    "label": _humanize(str(label)),
                    **pair,
                }
            )
        if len(changes) == 1:
            old_position = _position_text((change.get("base") or {}).get("at"))
            new_position = _position_text((change.get("head") or {}).get("at"))
            if (old_position or new_position) and old_position != new_position:
                push(
                    {
                        "id": f"{change.get('id')}:position",
                        "label": "Position",
                        "oldValue": old_position,
                        "newValue": new_position,
                    }
                )
        sheet_change = (change.get("details") or {}).get("sheetChange")
        if sheet_change:
            push(
                {
                    "id": f"{change.get('id')}:sheet",
                    "label": "Sheet",
                    "oldValue": sheet_change.get("old"),
                    "newValue": sheet_change.get("new"),
                }
            )
    for key, entry in aggregate.items():
        old_keys = sorted(entry["oldValues"].keys())
        new_keys = sorted(entry["newValues"].keys())
        if old_keys == new_keys:
            continue
        push(
            {
                "id": f"route-field:{key}",
                "label": _humanize(str(entry["label"])),
                "oldValue": ", ".join(old_keys) if old_keys else None,
                "newValue": ", ".join(new_keys) if new_keys else None,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Noise suppression (comparison-review-noise.ts)
# ---------------------------------------------------------------------------


def _local_net_name(value: str) -> str:
    parts = [part for part in value.split("/") if part]
    return parts[-1] if parts else value


def _rename_key(page: str, old_name: str, new_name: str) -> str:
    return "\u0000".join((page, _local_net_name(old_name), _local_net_name(new_name)))


def _semantic_net_rename_keys(changes: Iterable[Mapping[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for change in changes:
        if "net-renamed" not in (change.get("reasons") or []):
            continue
        pair = string_field_pair(change, ("name",))
        if pair:
            keys.add(_rename_key(page_for(change), pair["old"], pair["new"]))
    return keys


def _is_duplicate_native_label_rename(
    change: Mapping[str, Any], rename_keys: set[str]
) -> bool:
    if (
        change.get("domain") != "schematic"
        or change.get("kind") != "changed"
        or object_kind(change) not in SCHEMATIC_LABEL_KINDS
    ):
        return False
    pair = string_field_pair(change, ("Net", "Text"))
    if not pair or _rename_key(page_for(change), pair["old"], pair["new"]) not in rename_keys:
        return False
    return all(
        name.lower() in ("net", "text") for name, _ in meaningful_field_entries(change)
    )


def _net_rename_pair_key(old_name: str, new_name: str) -> str:
    return f"{old_name.strip()}\u0000{new_name.strip()}"


def semantic_net_renames(changes: Iterable[Mapping[str, Any]]) -> set[str]:
    """Net renames the schematic resolved, keyed old→new for the board pass."""

    keys: set[str] = set()
    for change in changes:
        if "net-renamed" not in (change.get("reasons") or []):
            continue
        pair = string_field_pair(change, ("name", "Net"))
        if pair:
            keys.add(_net_rename_pair_key(pair["old"], pair["new"]))
    return keys


def _derivative_net_rename(
    change: Mapping[str, Any], renames: set[str]
) -> Optional[dict[str, str]]:
    if change.get("domain") != "pcb" or change.get("kind") != "changed":
        return None
    reasons = change.get("reasons") or []
    if not reasons or any(reason != "net-changed" for reason in reasons):
        return None
    pair = string_field_pair(change, ("Net",))
    if not pair:
        return None
    if any(name.strip().lower() != "net" for name, _ in meaningful_field_entries(change)):
        return None
    if _net_rename_pair_key(pair["old"], pair["new"]) not in renames:
        return None
    return {"old": pair["old"], "new": pair["new"]}


def _is_generated_net_name(value: str) -> bool:
    return bool(re.match(r"^(?:unconnected-|net-\()", value.strip(), re.IGNORECASE))


def _is_generated_net_rename_only(change: Mapping[str, Any]) -> bool:
    reasons = change.get("reasons") or []
    if (
        change.get("domain") != "schematic"
        or "net-renamed" not in reasons
        or any(reason != "net-renamed" for reason in reasons)
    ):
        return False
    pair = string_field_pair(change, ("name",))
    return bool(
        pair
        and _is_generated_net_name(pair["old"])
        and _is_generated_net_name(pair["new"])
    )


_KIND_ORDER = {"removed": 0, "changed": 1, "added": 2}


def _change_position(change: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    targets = (change.get("details") or {}).get("targets") or []
    target_at = next((target.get("at") for target in targets if target.get("at")), None)
    return _first_not_none(
        (change.get("head") or {}).get("at"),
        (change.get("base") or {}).get("at"),
        target_at,
    )


def _compare_text(left: str, right: str) -> int:
    if left == right:
        return 0
    return -1 if left < right else 1


def review_order(left: Mapping[str, Any], right: Mapping[str, Any]) -> int:
    """Page, then what happened, then top-to-bottom/left-to-right, then id."""

    left_at = _change_position(left)
    right_at = _change_position(right)
    order = _compare_text(page_for(left), page_for(right))
    if order:
        return order
    order = _KIND_ORDER.get(str(left.get("kind")), 0) - _KIND_ORDER.get(
        str(right.get("kind")), 0
    )
    if order:
        return order
    if left_at and right_at:
        order = int(left_at["yNm"]) - int(right_at["yNm"])
        if order:
            return order
        order = int(left_at["xNm"]) - int(right_at["xNm"])
        if order:
            return order
    else:
        order = (0 if left_at else 1) - (0 if right_at else 1)
        if order:
            return order
    return _compare_text(str(left.get("id")), str(right.get("id")))


def prepare_changes_for_review(
    changes: Sequence[Mapping[str, Any]],
    *,
    net_renames: Optional[set[str]] = None,
) -> tuple[list[Mapping[str, Any]], int]:
    """Reviewer-level evidence from parser events, plus the suppressed count."""

    renames = net_renames or set()
    parent_symbol_events = {
        parent_symbol_key(change)
        for change in changes
        if change.get("domain") == "schematic" and object_kind(change) == "symbol"
    }
    parent_footprint_events = {
        parent_symbol_key(change)
        for change in changes
        if change.get("domain") == "pcb"
        and object_kind(change) == "footprint"
        and change.get("kind") in ("added", "removed")
    }
    net_rename_keys = _semantic_net_rename_keys(changes)
    prepared: list[Mapping[str, Any]] = []
    suppressed = 0

    for change in changes:
        kind = object_kind(change)
        if (
            has_structured_geometry_only_field(change)
            or _is_duplicate_native_label_rename(change, net_rename_keys)
            or _is_generated_net_rename_only(change)
        ):
            suppressed += 1
            continue
        if change.get("domain") == "schematic" and kind == "pin":
            if is_same_page_layout_only(change):
                suppressed += 1
                continue
            if (
                change.get("kind") in ("added", "removed")
                and parent_symbol_key(change) in parent_symbol_events
            ):
                suppressed += 1
                continue
            if (
                change.get("kind") == "changed"
                and parent_symbol_key(change) in parent_symbol_events
                and all(
                    name.strip().lower() == "reference"
                    for name, _ in meaningful_field_entries(change)
                )
            ):
                suppressed += 1
                continue

        if (
            change.get("domain") == "pcb"
            and kind in PCB_COMPONENT_CHILD_KINDS
            and (kind != "zone" or has_native_parent(change))
            and change.get("kind") in ("added", "removed")
            and parent_symbol_key(change) in parent_footprint_events
        ):
            suppressed += 1
            continue

        renamed = _derivative_net_rename(change, renames)
        if renamed:
            prepared.append(
                {
                    **change,
                    "classification": "secondary",
                    "derivedFrom": {"kind": "net-rename", **renamed},
                }
            )
        elif (
            change.get("domain") == "schematic"
            and kind in SCHEMATIC_WIRING_KINDS
            and not change.get("net")
            and change.get("kind") in ("added", "removed")
        ):
            prepared.append({**change, "classification": "secondary"})
        elif is_same_page_layout_only(change):
            prepared.append({**change, "classification": "secondary"})
        else:
            prepared.append(change)

    return sorted(prepared, key=cmp_to_key(review_order)), suppressed


# ---------------------------------------------------------------------------
# Part identity and net aliases (`comparison-review-groups.ts` indexes)
# ---------------------------------------------------------------------------

_MPN_FIELD_PATTERN = re.compile(
    r"^(mpn|(manufacturer|mfr\.?|mfg\.?)[\s_-]*part[\s_-]*(number|no\.?)?)$",
    re.IGNORECASE,
)
_VALUE_FIELD_PATTERN = re.compile(r"^value$", re.IGNORECASE)


class _PartIdentityIndex:
    """The Value and MPN a designator carried on one side, when the BOM knows."""

    __slots__ = ("_rows", "_mpn_field")

    def __init__(self, bom: Optional[Mapping[str, Any]]) -> None:
        fields = (bom or {}).get("fields")
        self._mpn_field = next(
            (field for field in (fields or []) if _MPN_FIELD_PATTERN.match(str(field).strip())),
            None,
        )
        self._rows = {
            row.get("ref"): row for row in ((bom or {}).get("changes") or [])
        }

    def resolve(self, reference: str, side: str) -> Optional[dict[str, str]]:
        row = self._rows.get(reference)
        fields = row.get("old" if side == "old" else "new") if row else None
        if not isinstance(fields, Mapping):
            return None
        value = _text(fields.get("Value")).strip()
        mpn = _text(fields.get(self._mpn_field) if self._mpn_field else None).strip()
        if not value and not mpn:
            return None
        return {"value": value, "mpn": mpn}


def _part_side_from_change(
    change: Mapping[str, Any], side: str
) -> Optional[dict[str, str]]:
    def read(pattern: re.Pattern[str]) -> str:
        for name, raw in (change.get("fields") or {}).items():
            if not pattern.match(str(name).strip()):
                continue
            sides = field_sides(raw)
            picked = sides.get(side) if sides else raw
            if isinstance(picked, str) and picked.strip():
                return picked.strip()
        return ""

    value = read(_VALUE_FIELD_PATTERN)
    mpn = read(_MPN_FIELD_PATTERN)
    if not value and not mpn:
        return None
    return {"value": value, "mpn": mpn}


def _part_side_key(side: Optional[Mapping[str, str]]) -> str:
    if not side:
        return ""
    return f"{(side.get('value') or '').lower()}\u0000{(side.get('mpn') or '').lower()}"


def _part_side_label(side: Optional[Mapping[str, str]]) -> str:
    if not side:
        return ""
    return side.get("value") or side.get("mpn") or ""


def _part_transition(
    change: Mapping[str, Any],
    reference: str,
    parts: Optional[_PartIdentityIndex],
) -> Optional[dict[str, Any]]:
    def side_for(side: str) -> Optional[dict[str, str]]:
        resolved = parts.resolve(reference, side) if parts else None
        return resolved if resolved is not None else _part_side_from_change(change, side)

    before = None if change.get("kind") == "added" else side_for("old")
    after = None if change.get("kind") == "removed" else side_for("new")
    if not before and not after:
        return None

    before_label = _part_side_label(before)
    after_label = _part_side_label(after)
    label = (
        f"{before_label} → {after_label}"
        if before and after and _part_side_key(before) != _part_side_key(after)
        else after_label or before_label
    )
    return {
        "bucket": "\u0001".join(("part", _part_side_key(before), _part_side_key(after))),
        "id": ">".join(
            (
                "part",
                f"{before_label}~{before.get('mpn') if before else ''}",
                f"{after_label}~{after.get('mpn') if after else ''}",
            )
        ),
        "label": label,
        "mpn": (
            (after.get("mpn") if after else None)
            or (before.get("mpn") if before else None)
            or None
        ),
    }


def _component_identity(
    change: Mapping[str, Any],
    reference: str,
    category: str,
    parts: Optional[_PartIdentityIndex],
    fallback_id_identity: str,
) -> dict[str, Any]:
    part = _part_transition(change, reference, parts)
    if not part:
        return {
            "bucket": f"component:{reference}",
            "idIdentity": fallback_id_identity,
            "category": category,
            "label": reference,
        }
    return {
        "bucket": part["bucket"],
        "idIdentity": part["id"],
        "category": category,
        "label": part["label"],
        "partMpn": part["mpn"],
    }


def _build_net_aliases(changes: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    canonical: dict[str, str] = {}
    for change in changes:
        if change.get("domain") != "pcb":
            continue
        if object_kind(change) not in PCB_COPPER_KINDS:
            continue
        pair = string_field_pair(change, ("Net", "name"))
        if not pair:
            continue
        new_name = pair["new"].strip()
        old_name = pair["old"].strip()
        if not new_name or not old_name or new_name == old_name:
            continue
        canonical[new_name] = old_name

    def resolve(name: str) -> str:
        seen = {name}
        current = name
        while current in canonical:
            following = canonical[current]
            if following in seen:
                break
            seen.add(following)
            current = following
        return current

    return {name: resolve(name) for name in canonical}


def _canonical_net_name(
    change: Mapping[str, Any], aliases: Optional[Mapping[str, str]]
) -> Optional[str]:
    pair = string_field_pair(change, ("Net", "name"))
    own = _text(pair["old"] if pair else change.get("net")).strip()
    if not own:
        return None
    return aliases.get(own, own) if aliases else own


def _build_derived_net_renames(
    changes: Iterable[Mapping[str, Any]]
) -> dict[str, dict[str, str]]:
    renames: dict[str, dict[str, str]] = {}
    for change in changes:
        derived = change.get("derivedFrom") or {}
        if derived.get("kind") != "net-rename":
            continue
        old_name = _text(derived.get("old")).strip()
        new_name = _text(derived.get("new")).strip()
        if not old_name or not new_name or old_name == new_name:
            continue
        renames[old_name] = {"old": old_name, "new": new_name}
    return renames
