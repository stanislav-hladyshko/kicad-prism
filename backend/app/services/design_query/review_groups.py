"""Design Comparison's review queue, computed server-side (ADR §3.8).

This is the port of the frontend's review pipeline:

- ``review_evidence.prepare_changes_for_review`` turns parser events into
  reviewer-level evidence, the way `comparison-review-noise.ts` does;
- identity and bucketing decide which changes share one review row, the way
  `comparison-review-groups.ts` does;
- queue metadata names the owner, section and one-line summary, the way
  `comparison-review-queue.ts` does.

``fixtures/review-groups/`` is the shared fixture: the frontend's
``review-groups-fixture.test.ts`` and the backend's
``test_design_compare_review.py`` both reproduce ``expected.json``. A change
here must keep the TypeScript and Python answers equal.

Comments are not part of the server answer yet: :func:`review_groups` passes
none, so every group reports an unresolved count of zero and the fixture is the
queue a reviewer sees before any comment is attached.

The review document stores nanometres. Only displayed coordinates convert to
millimetres, as the frontend does.
"""

from __future__ import annotations

import math
import re
from functools import cmp_to_key
from typing import Any, Mapping, Optional, Sequence

from app.services.design_query.review_evidence import (
    CATEGORY_ORDER,
    PCB_COMPONENT_CHILD_KINDS,
    PCB_COPPER_KINDS,
    RULE_KINDS,
    SCHEMATIC_WIRING_KINDS,
    _CHANGE_KINDS,
    _PartIdentityIndex,
    _build_derived_net_renames,
    _build_net_aliases,
    _canonical_net_name,
    _change_position,
    _component_identity,
    _first_not_none,
    _js_string,
    _locale_compare,
    _natural_key,
    _point_mm,
    _text,
    change_layers,
    compact_value,
    format_value,
    is_same_page_layout_only,
    meaningful_field_entries,
    object_kind,
    page_for,
    prepare_changes_for_review,
    property_deltas,
    reference_for,
    semantic_category,
    semantic_net_renames,
    union_layers,
)


def _net_rename_identity(old_name: str, new_name: str) -> dict[str, Any]:
    return {
        "bucket": f"net-rename:{old_name} {new_name}",
        "idIdentity": f"net-rename:{old_name} {new_name}",
        "category": "nets",
        "label": new_name,
    }


def _identity_for(
    change: Mapping[str, Any],
    parts: Optional[_PartIdentityIndex],
    net_aliases: Optional[Mapping[str, str]],
    derived_net_renames: Optional[Mapping[str, dict[str, str]]],
) -> dict[str, Any]:
    kind = object_kind(change)
    page = page_for(change)
    category = semantic_category(change)
    exact_identity = _first_not_none(
        change.get("semanticId"),
        reference_for(change),
        change.get("net"),
        change.get("id"),
    )

    if category == "rules":
        rule_groups = {
            "drc-exclusion": ("drc-exclusions", "DRC exclusions"),
            "erc-exclusion": ("erc-exclusions", "ERC exclusions"),
            "erc-pin-rule": ("erc-pin-compatibility", "ERC pin compatibility"),
            "net-class-assignment": ("net-class-assignments", "Net class assignments"),
        }
        if kind in rule_groups:
            bucket, label = rule_groups[kind]
            return {
                "bucket": bucket,
                "idIdentity": bucket,
                "category": category,
                "label": label,
            }
        return {
            "bucket": f"rule:{kind}:{change.get('label')}",
            "idIdentity": exact_identity,
            "category": category,
            "label": change.get("label"),
        }

    derived = change.get("derivedFrom") or {}
    if derived.get("kind") == "net-rename":
        return _net_rename_identity(
            _text(derived.get("old")), _text(derived.get("new"))
        )

    if is_same_page_layout_only(change):
        return {
            "bucket": f"layout:{page}",
            "idIdentity": f"layout:{page}",
            "category": "graphics",
            "label": "Layout-only changes",
        }

    if change.get("domain") == "schematic":
        if (
            change.get("net")
            and category == "nets"
            and kind not in ("no-connect", "sheet-pin")
        ):
            net = change.get("net")
            return {
                "bucket": f"net:{net}",
                "idIdentity": f"net:{net}",
                "category": "nets",
                "label": net,
            }
        if (
            category != "nets"
            and change.get("reference")
            and (kind == "symbol" or category in ("components", "symbols"))
        ):
            return _component_identity(
                change,
                change.get("reference"),
                category,
                parts,
                exact_identity,
            )
        if (
            kind in SCHEMATIC_WIRING_KINDS
            and not change.get("net")
            and change.get("kind") in ("added", "removed")
        ):
            return {
                "bucket": f"wiring:{page}",
                "idIdentity": f"wiring:{page}",
                "category": "nets",
                "label": "Wiring changes",
            }
        if kind == "no-connect" and change.get("kind") != "changed":
            return {
                "bucket": f"no-connect:{page}",
                "idIdentity": f"no-connect:{page}",
                "category": "nets",
                "label": "No-connect markers",
            }
        if kind == "sheet-pin":
            return {
                "bucket": f"sheet-pins:{page}",
                "idIdentity": f"sheet-pins:{page}",
                "category": "sheets",
                "label": "Hierarchical sheet pins",
            }

    if change.get("domain") == "pcb":
        reference = reference_for(change)
        if reference and (
            kind == "footprint" or semantic_category(change) == "components"
        ):
            return _component_identity(
                change, reference, "components", parts, exact_identity
            )
        if reference and kind in PCB_COMPONENT_CHILD_KINDS:
            return _component_identity(
                change, reference, category, parts, f"component:{reference}"
            )
        if category == "zones":
            layer = (
                ", ".join(union_layers(change))
                or (change.get("head") or {}).get("layer")
                or (change.get("base") or {}).get("layer")
                or None
            )
            zone_name = _text(change.get("net")).strip() or "Unconnected"
            return {
                "bucket": f"zone:{exact_identity}",
                "idIdentity": exact_identity,
                "category": category,
                "label": f"{zone_name} zone{f' · {layer}' if layer else ''}",
            }
        if kind in PCB_COPPER_KINDS:
            canonical = _canonical_net_name(change, net_aliases)
            current = _text(change.get("net")).strip()
            owned_rename = (
                (derived_net_renames or {}).get(canonical) if canonical else None
            ) or ((derived_net_renames or {}).get(current) if current else None)
            if owned_rename:
                return _net_rename_identity(
                    owned_rename["old"], owned_rename["new"]
                )
            net = canonical or "Unconnected copper"
            label = (
                f"{canonical} → {current} routing"
                if canonical and current and current != canonical
                else f"{canonical} routing"
                if canonical
                else net
            )
            return {
                "bucket": f"copper:{net}",
                "idIdentity": f"net:{net}",
                "category": "nets",
                "label": label,
            }
        if category == "graphics":
            layer = ", ".join(union_layers(change)) or "Documentation"
            folded_layer = layer.lower()
            if "edge.cuts" in folded_layer:
                return {
                    "bucket": "board-outline",
                    "idIdentity": "board-outline",
                    "category": category,
                    "label": "Board outline",
                }
            has_text = any(
                str(field).lower() == "text"
                for field in (change.get("fields") or {})
            )
            if (
                (".fab" in folded_layer or folded_layer in ("f.fab", "b.fab"))
                and has_text
            ):
                return {
                    "bucket": "fabrication-notes",
                    "idIdentity": "fabrication-notes",
                    "category": category,
                    "label": "Fabrication notes",
                }
            readable_kind = re.sub(r"[-_]", " ", kind)
            return {
                "bucket": f"graphics:{layer}:{kind}",
                "idIdentity": f"graphics:{layer}:{kind}",
                "category": category,
                "label": f"{layer} {readable_kind}",
            }

    return {
        "bucket": f"exact:{exact_identity}",
        "idIdentity": exact_identity,
        "category": category,
        "label": _first_not_none(
            reference_for(change), change.get("net"), change.get("label")
        ),
    }


def _merged_kind(kinds: Iterable[str]) -> str:
    unique = set(kinds)
    if len(unique) == 1:
        return next(iter(unique))
    return "changed"


def create_grouping_context(
    changes: Sequence[Mapping[str, Any]],
    bom: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """The part index, net aliases and rename map one domain's groups share."""

    return {
        "parts": _PartIdentityIndex(bom),
        "netAliases": _build_net_aliases(changes),
        "derivedNetRenames": _build_derived_net_renames(changes),
    }


def _collision_key(group: Mapping[str, Any]) -> str:
    return f"{group.get('kind')}:{group.get('label')}"


def _open_comment_count(
    comments: Sequence[Mapping[str, Any]], group_id: str
) -> int:
    return sum(
        1
        for comment in comments
        if comment.get("status") == "OPEN"
        and (
            comment.get("semanticItemId") == group_id
            or _text(comment.get("semanticItemId")).startswith(f"{group_id}::")
        )
    )


def group_changes(
    changes: Sequence[Mapping[str, Any]],
    comments: Optional[Sequence[Mapping[str, Any]]] = None,
    grouping: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    """One authored decision, one review row, in the queue's own order."""

    attached_comments = comments or []
    context = grouping or create_grouping_context(changes)
    parts = context["parts"]
    net_aliases = context["netAliases"]
    derived_net_renames = context["derivedNetRenames"]
    buckets: dict[str, dict[str, Any]] = {}

    for change in changes:
        identity = _identity_for(change, parts, net_aliases, derived_net_renames)
        key = f"{change.get('domain')}:{identity.get('category')}:{identity.get('bucket')}"
        existing = buckets.get(key)
        if existing:
            existing["changes"].append(change)
            if change.get("classification") != "secondary":
                existing["classification"] = "primary"
            continue
        group_id = f"{change.get('domain')}:{identity.get('category')}:{identity.get('idIdentity')}"
        buckets[key] = {
            "id": group_id,
            "category": identity.get("category"),
            "kind": change.get("kind"),
            "label": identity.get("label"),
            "classification": _first_not_none(
                change.get("classification"), "primary"
            ),
            "unresolvedCount": _open_comment_count(attached_comments, group_id),
            "changes": [change],
            "references": [],
            "partMpn": identity.get("partMpn"),
        }

    groups = list(buckets.values())
    for group in groups:
        group["kind"] = _merged_kind(change.get("kind") for change in group["changes"])
        references = {
            reference_for(change)
            for change in group["changes"]
            if reference_for(change)
        }
        group["references"] = sorted(references, key=_natural_key)

    # Two parts can read alike when only their MPN differs; qualify the labels
    # that actually collide so the common row stays short.
    label_counts: dict[str, int] = {}
    for group in groups:
        label_counts[_collision_key(group)] = label_counts.get(_collision_key(group), 0) + 1
    for group in groups:
        if not group.get("partMpn"):
            continue
        if label_counts.get(_collision_key(group), 0) < 2:
            continue
        group["label"] = f"{group['label']} ({group['partMpn']})"

    # Identical native objects can share a net and layer; qualify with the first
    # concrete property transition, then with a coordinate.
    qualified_counts: dict[str, int] = {}
    for group in groups:
        qualified_counts[_collision_key(group)] = qualified_counts.get(
            _collision_key(group), 0
        ) + 1
    for group in groups:
        if qualified_counts.get(_collision_key(group), 0) < 2:
            continue
        deltas = property_deltas(group["changes"])
        delta = None
        if group.get("kind") == "changed":
            delta = next(
                (
                    candidate
                    for candidate in deltas
                    if str(candidate.get("label")).lower()
                    in ("position", "priority", "layer", "net")
                ),
                None,
            ) or next(
                (
                    candidate
                    for candidate in deltas
                    if all(
                        value is None
                        or isinstance(value, (str, int, float, bool))
                        for value in (candidate.get("oldValue"), candidate.get("newValue"))
                    )
                    and not any(
                        "${" in _text(value)
                        for value in (candidate.get("oldValue"), candidate.get("newValue"))
                    )
                ),
                None,
            )
        if delta:
            group["label"] = (
                f"{group['label']} · {delta.get('verb')} "
                f"{format_value(delta.get('oldValue'))}→{format_value(delta.get('newValue'))}"
            )
            continue
        change = group["changes"][0]
        point = _change_position(change)
        if point:
            at = _point_mm(point)
            coordinate = [_coordinate(value) for value in at]
            group["label"] = f"{group['label']} · at {coordinate[0]}, {coordinate[1]}"

    remaining: dict[str, list[dict[str, Any]]] = {}
    for group in groups:
        remaining.setdefault(_collision_key(group), []).append(group)
    for bucket in remaining.values():
        if len(bucket) < 2:
            continue
        for index, group in enumerate(bucket):
            group["label"] = f"{group['label']} · item {index + 1}"

    def compare(left: Mapping[str, Any], right: Mapping[str, Any]) -> int:
        if left.get("classification") != right.get("classification"):
            return -1 if left.get("classification") == "primary" else 1
        order = CATEGORY_ORDER.get(str(left.get("category")), 9) - CATEGORY_ORDER.get(
            str(right.get("category")), 9
        )
        if order:
            return order
        return _locale_compare(_text(left.get("label")), _text(right.get("label")))

    return sorted(groups, key=cmp_to_key(compare))


def _coordinate(value: float) -> str:
    scaled = value * 100
    rounded = math.floor(scaled + 0.5) / 100 if value >= 0 else math.ceil(scaled - 0.5) / 100
    return _js_string(rounded)


# ---------------------------------------------------------------------------
# Queue metadata (comparison-review-queue.ts)
# ---------------------------------------------------------------------------

REVIEW_IMPACT_LABELS: dict[str, str] = {
    "mechanical": "Mechanical",
    "fabrication": "PCB fabrication",
    "electrical": "Electrical",
    "assembly": "Assembly / BOM",
    "constraints": "Rules / constraints",
    "documentation": "Documentation",
}

_FABRICATION_LAYERS = (
    ".cu",
    ".mask",
    ".paste",
    ".silks",
    ".fab",
    ".crtyd",
    "edge.cuts",
)

_ASSEMBLY_FIELD_NAMES = (
    "value",
    "footprint",
    "description",
    "datasheet",
    "part number",
    "manufacturer",
    "dnp",
    "in bom",
    "exclude from bom",
    "substitution",
    "board only",
    "position file",
)

_SPATIAL_REASON_CODES = frozenset(
    ("moved", "rotated", "mirrored", "layer-changed", "lib-changed")
)

QUEUE_SECTION_LABEL: dict[str, str] = {
    "components": "Components",
    "nets": "Nets",
    "rules": "Rules & constraints",
    "layout": "Layout & documentation",
}

QUEUE_SECTION_ORDER: tuple[str, ...] = ("components", "nets", "rules", "layout")

_SECTION_BY_CATEGORY: dict[str, str] = {
    "components": "components",
    "symbols": "components",
    "sheets": "components",
    "nets": "nets",
    "zones": "nets",
    "rules": "rules",
}


def _has_fabrication_layer(change: Mapping[str, Any]) -> bool:
    return any(
        suffix in layer.lower()
        for layer in change_layers(change)
        for suffix in _FABRICATION_LAYERS
    )


def _has_assembly_evidence(change: Mapping[str, Any]) -> bool:
    return any(
        name in str(field).strip().lower()
        for field in (change.get("fields") or {})
        for name in _ASSEMBLY_FIELD_NAMES
    ) or "dnp-changed" in (change.get("reasons") or [])


def review_impact_for_group(group: Mapping[str, Any]) -> str:
    """Which discipline owns the proof behind one review row."""

    changes = group.get("changes") or []
    kinds = {object_kind(change) for change in changes}
    if group.get("classification") == "secondary":
        return "documentation"
    if group.get("category") == "rules" or any(kind in RULE_KINDS for kind in kinds):
        return "constraints"

    reasons = {reason for change in changes for reason in (change.get("reasons") or [])}
    layers = [
        layer.lower()
        for change in changes
        for layer in change_layers(change)
    ]
    mechanical = (
        group.get("label") == "Board outline"
        or any("edge.cuts" in layer or "margin" in layer for layer in layers)
        or any(
            re.search(
                r"keepout|courtyard|mounting hole|board outline",
                str(field),
                re.IGNORECASE,
            )
            for change in changes
            for field in (change.get("fields") or {})
        )
    )
    if mechanical:
        return "mechanical"

    pcb_changes = [change for change in changes if change.get("domain") == "pcb"]
    if pcb_changes:
        net_assignment_only = (
            bool(reasons)
            and all(
                reason in ("net-changed", "renamed", "properties-changed")
                for reason in reasons
            )
            and all(
                (change.get("fields") or {})
                and all(
                    str(field).strip().lower() == "net"
                    for field in (change.get("fields") or {})
                )
                for change in changes
            )
        )
        if net_assignment_only or (
            group.get("category") == "nets"
            and not any(reason in _SPATIAL_REASON_CODES for reason in reasons)
        ):
            return "electrical"
        if (
            any(
                kind in PCB_COPPER_KINDS
                or kind in PCB_COMPONENT_CHILD_KINDS
                or kind == "footprint"
                for kind in kinds
            )
            or any(_has_fabrication_layer(change) for change in pcb_changes)
            or any(reason in _SPATIAL_REASON_CODES for reason in reasons)
        ):
            return "fabrication"
        if any(_has_assembly_evidence(change) for change in pcb_changes) or group.get(
            "category"
        ) == "components":
            return "assembly"
        return "fabrication"

    if group.get("category") in ("nets", "sheets"):
        return "electrical"
    if any(_has_assembly_evidence(change) for change in changes):
        return "assembly"
    if group.get("category") in ("components", "symbols"):
        return "electrical"
    return "documentation"


def _status_summary(changes: Sequence[Mapping[str, Any]]) -> str:
    counts = {"added": 0, "removed": 0, "changed": 0}
    for change in changes:
        kind = change.get("kind")
        if kind in counts:
            counts[kind] += 1
    return " · ".join(
        part
        for part in (
            f"{counts['added']} added" if counts["added"] else "",
            f"{counts['removed']} removed" if counts["removed"] else "",
            f"{counts['changed']} modified" if counts["changed"] else "",
        )
        if part
    )


def _kind_noun(kind: str, count: int = 1) -> str:
    row = _CHANGE_KINDS.get(kind)
    noun = row[0] if row else re.sub(r"[-_]", " ", kind)
    return noun if count == 1 else f"{noun}s"


def _kind_summary(changes: Sequence[Mapping[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for change in changes:
        kind = object_kind(change)
        counts[kind] = counts.get(kind, 0) + 1
    ordered = sorted(counts.items(), key=lambda entry: -entry[1])[:3]
    return " · ".join(f"{count} {_kind_noun(kind, count)}" for kind, count in ordered)


def group_document_entries(group: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for change in group.get("changes") or []:
        paths: set[str] = set()
        candidates = [
            change.get("page"),
            (change.get("base") or {}).get("documentPath"),
            (change.get("head") or {}).get("documentPath"),
            *[
                target.get("documentPath")
                for target in ((change.get("details") or {}).get("targets") or [])
            ],
        ]
        for path in candidates:
            if path:
                paths.add(path)
        for document_path in paths:
            existing = entries.get(document_path)
            if existing:
                existing["count"] += 1
            else:
                entries[document_path] = {
                    "documentPath": document_path,
                    "count": 1,
                    "change": change,
                }
    return sorted(
        entries.values(),
        key=cmp_to_key(
            lambda left, right: _locale_compare(
                left["documentPath"], right["documentPath"]
            )
        ),
    )


def group_summary(group: Mapping[str, Any]) -> str:
    changes = group.get("changes") or []
    pages = group_document_entries(group)
    has_added = any(change.get("kind") == "added" for change in changes)
    has_removed = any(change.get("kind") == "removed" for change in changes)
    references = {change.get("reference") for change in changes if change.get("reference")}
    if has_added and has_removed and len(references) == 1 and len(pages) > 1:
        old_page = next(change for change in changes if change.get("kind") == "removed")
        new_page = next(change for change in changes if change.get("kind") == "added")
        return f"Moved: {page_for(old_page)} → {page_for(new_page)}"

    derived = (changes[0].get("derivedFrom") if changes else None) or {}
    if derived.get("kind") == "net-rename":
        objects = len(changes)
        return (
            f"Renamed from {derived.get('old')} · {objects} board object"
            f"{'' if objects == 1 else 's'} follow the schematic"
        )

    if group.get("label") == "Layout-only changes":
        return _kind_summary(changes)
    if group.get("category") == "rules" and len(changes) > 1:
        return _status_summary(changes)

    def rank(change: Mapping[str, Any]) -> int:
        kind = object_kind(change)
        if kind in ("symbol", "footprint"):
            return 0
        if kind == "pad":
            return 1
        if kind in PCB_COMPONENT_CHILD_KINDS:
            return 2
        return 1

    for change in sorted(changes, key=rank):
        details = change.get("details") or {}
        if "connectivity-changed" in (change.get("reasons") or []) and details.get(
            "connectivity"
        ):
            connectivity = details["connectivity"]
            terminals = [
                f"+{value}" for value in connectivity.get("addedTerminals") or []
            ] + [
                f"−{value}" for value in connectivity.get("removedTerminals") or []
            ]
            return ", ".join(terminals) or "Connectivity changed"
        if "instance-replaced" in (change.get("reasons") or []):
            return "Instance replaced (same RefDes)"
        if "instance-count-changed" in (change.get("reasons") or []) and details.get(
            "instanceCount"
        ):
            delta = details["instanceCount"]
            return f"Instances {delta.get('old')} → {delta.get('new')}"
        if "label-count-changed" in (change.get("reasons") or []) and details.get(
            "labelInstances"
        ):
            delta = details["labelInstances"]
            return f"Labels {delta.get('old')} → {delta.get('new')}"
        if "sheet-changed" in (change.get("reasons") or []) and details.get(
            "sheetChange"
        ):
            delta = details["sheetChange"]
            old_sheet = _first_not_none(delta.get("old"), "—")
            new_sheet = _first_not_none(delta.get("new"), "—")
            return f"Sheet {old_sheet} → {new_sheet}"
        fields = [
            (name, sides)
            for name, sides in meaningful_field_entries(change)
            if not (
                group.get("category") == "components"
                and object_kind(change) in PCB_COMPONENT_CHILD_KINDS
                and name.lower() in ("reference", "layer", "stroke")
            )
        ][:2]
        if fields:
            return " · ".join(
                f"{name}: {compact_value(sides.get('old'))} → {compact_value(sides.get('new'))}"
                for name, sides in fields
            )

    if len(changes) > 1:
        statuses = _status_summary(changes)
        kinds = {object_kind(change) for change in changes}
        return f"{statuses} · {_kind_summary(changes)}" if len(kinds) > 1 else statuses
    reasons = (changes[0].get("reasons") if changes else None) or []
    if "net-changed" in reasons:
        return "Net assignment changed"
    if "lib-changed" in reasons:
        return "Library item changed"
    if "renamed" in reasons:
        return "Renamed"
    if "re-pathed" in reasons:
        return "Hierarchy path changed"
    return ""


def queue_section(group: Mapping[str, Any]) -> str:
    if group.get("classification") == "secondary":
        return "layout"
    return _SECTION_BY_CATEGORY.get(str(group.get("category")), "layout")


# ---------------------------------------------------------------------------
# The server answer
# ---------------------------------------------------------------------------


def review_groups(result: Mapping[str, Any], domain: str) -> list[dict[str, Any]]:
    """The review queue for one domain of a comparison result.

    ``result`` is the stored ``prism.design_compare.result.a1`` document (or the
    same document assembled from a v2 manifest's sidecars). ``domain`` is
    ``sch`` or ``pcb``; the board pass receives the schematic's semantic net
    renames, as the workspace passes them.

    Comments are not part of the server answer yet: none are passed, so a group's
    unresolved count is always zero.

    Each group is ``{id, kind, title, summary, impact, section, changeIds}``,
    in the queue's order. ``title`` is the group's ``label``; ``changeIds`` is
    sorted because a group's evidence is a set, not a sequence.
    """

    if domain not in ("sch", "pcb"):
        raise ValueError("domain must be 'sch' or 'pcb'")
    schematic_changes = ((result.get("schematic") or {}).get("changes")) or []
    pcb_changes = ((result.get("pcb") or {}).get("changes")) or []
    bom = result.get("bom")
    if domain == "sch":
        changes, _ = prepare_changes_for_review(schematic_changes)
    else:
        changes, _ = prepare_changes_for_review(
            pcb_changes, net_renames=semantic_net_renames(schematic_changes)
        )
    groups = group_changes(changes, [], create_grouping_context(changes, bom))
    return [
        {
            "id": group["id"],
            "kind": group["kind"],
            "title": group["label"],
            "summary": group_summary(group),
            "impact": review_impact_for_group(group),
            "section": queue_section(group),
            "changeIds": sorted(change.get("id") for change in group["changes"]),
        }
        for group in groups
    ]
