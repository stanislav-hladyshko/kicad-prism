"""Highlight boxes for ``render_view``, in millimetres on the view's frame.

Every box here is a plain mm rectangle on the page (schematic) or board frame
(PCB). The projections are the only source of geometry:

* ``projections/placements.json`` gives a footprint's board-absolute anchor,
  its rotation, its side and its footprint-local courtyard (field map
  ``placements.md``). A PCB component's box is the courtyard rotated into the
  board frame; without one it is the extent of the footprint's pads.
* ``projections/board-objects.json`` gives schematic symbols and pins with
  ``at`` on the page. A component's box is the extent of its symbol anchor and
  pins, plus a margin (``board-objects.md``).
* ``projections/semantic-index.json`` joins a net to its pins through
  ``terminals`` (``index.net``); the pin objects supply the points.

KiCad rotates a footprint's local point ``(lx, ly)`` to
``(lx·cosθ + ly·sinθ, ly·cosθ − lx·sinθ)`` and adds the anchor (placements.md
R9). A back-side footprint uses the same transform: KiCad mirrors the drawing,
not the footprint frame, and the field map's pad rule does not depend on side.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

NM_PER_MM = 1_000_000.0
#: A schematic symbol is a point in board-objects (its pins carry the anchor
#: too), so its box is the anchor plus this margin on every side.
SCHEMATIC_MARGIN_MM = 2.54
#: 15% of the union's longest side, on every side.
FIT_MARGIN = 0.15
#: A fitted crop never gets smaller than this, so a single-pin net is visible.
MIN_REGION_MM = 5.0


@dataclass(frozen=True)
class Box:
    """An axis-aligned rectangle in millimetres, y down."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    @property
    def center(self) -> tuple[float, float]:
        """The point a marker comment is anchored at: the box's centre."""

        return (self.min_x + self.max_x) / 2, (self.min_y + self.max_y) / 2

    def union(self, other: "Box") -> "Box":
        return Box(
            min(self.min_x, other.min_x),
            min(self.min_y, other.min_y),
            max(self.max_x, other.max_x),
            max(self.max_y, other.max_y),
        )

    def padded(self, margin: float) -> "Box":
        return Box(self.min_x - margin, self.min_y - margin, self.max_x + margin, self.max_y + margin)

    def at_least(self, size: float) -> "Box":
        """``self`` grown about its centre until both sides reach ``size``."""

        width, height = max(self.width, size), max(self.height, size)
        centre_x, centre_y = (self.min_x + self.max_x) / 2, (self.min_y + self.max_y) / 2
        return Box(
            centre_x - width / 2,
            centre_y - height / 2,
            centre_x + width / 2,
            centre_y + height / 2,
        )

    def as_region(self) -> dict[str, float]:
        return {
            "x": round(self.min_x, 6),
            "y": round(self.min_y, 6),
            "width": round(self.width, 6),
            "height": round(self.height, 6),
        }


@dataclass(frozen=True)
class Highlight:
    """One requested highlight, whether it was found, and its box.

    ``known`` differs from "has a box" for a 3D render, where a component or
    net can be found without any box being drawn (D9: no 3D highlights).
    """

    kind: str
    key: str
    box: Box | None
    known: bool | None = None

    @property
    def found(self) -> bool:
        return self.known if self.known is not None else self.box is not None

    def as_record(self) -> dict[str, Any]:
        return {"kind": self.kind, "key": self.key, "found": self.found}


def union(boxes: Iterable[Box]) -> Box | None:
    result: Box | None = None
    for box in boxes:
        result = box if result is None else result.union(box)
    return result


def fit_region(boxes: Sequence[Box], whole: Box, *, margin: float = FIT_MARGIN) -> Box:
    """The union of ``boxes`` with a margin, or ``whole`` when there are none."""

    found = union(boxes)
    if found is None:
        return whole.at_least(MIN_REGION_MM)
    pad = margin * max(found.width, found.height)
    return found.padded(pad).at_least(MIN_REGION_MM)


def _mm(value: Any) -> float:
    return float(value or 0) / NM_PER_MM


def _point(value: Mapping[str, Any] | None) -> tuple[float, float] | None:
    if not isinstance(value, Mapping):
        return None
    if "xNm" in value or "yNm" in value:
        return _mm(value.get("xNm")), _mm(value.get("yNm"))
    return None


def rotate_local(x: float, y: float, rotation_deg: float) -> tuple[float, float]:
    """Rotate a footprint-local point into the board frame (placements.md R9)."""

    if not rotation_deg:
        return x, y
    radians = math.radians(rotation_deg)
    cosine, sine = math.cos(radians), math.sin(radians)
    return x * cosine + y * sine, y * cosine - x * sine


def _rotate_box(box: Box, rotation_deg: float) -> Box:
    corners = [
        rotate_local(box.min_x, box.min_y, rotation_deg),
        rotate_local(box.max_x, box.min_y, rotation_deg),
        rotate_local(box.max_x, box.max_y, rotation_deg),
        rotate_local(box.min_x, box.max_y, rotation_deg),
    ]
    return Box(
        min(point[0] for point in corners),
        min(point[1] for point in corners),
        max(point[0] for point in corners),
        max(point[1] for point in corners),
    )


def _translate(box: Box, dx: float, dy: float) -> Box:
    return Box(box.min_x + dx, box.min_y + dy, box.max_x + dx, box.max_y + dy)


def courtyard_box(footprint: Mapping[str, Any]) -> Box | None:
    """A footprint's courtyard rotated and anchored to the board frame."""

    bounds = footprint.get("courtyardBounds")
    point = _point(footprint.get("positionNm"))
    if not isinstance(bounds, Mapping) or point is None:
        return None
    minimum, maximum = _point(bounds.get("minNm")), _point(bounds.get("maxNm"))
    if minimum is None or maximum is None:
        return None
    local = Box(minimum[0], minimum[1], maximum[0], maximum[1])
    rotated = _rotate_box(local, float(footprint.get("rotationDeg") or 0))
    return _translate(rotated, point[0], point[1])


def pad_box(footprint: Mapping[str, Any], pad: Mapping[str, Any]) -> Box | None:
    """One pad's board-absolute extent (its own rotation is not in the member)."""

    point = _point(pad.get("positionNm"))
    size = pad.get("size")
    if point is None or not isinstance(size, Mapping):
        return None
    half_x, half_y = _mm(size.get("xNm")) / 2, _mm(size.get("yNm")) / 2
    rotation = float(footprint.get("rotationDeg") or 0)
    radians = math.radians(rotation)
    extent_x = abs(math.cos(radians)) * half_x + abs(math.sin(radians)) * half_y
    extent_y = abs(math.sin(radians)) * half_x + abs(math.cos(radians)) * half_y
    return Box(point[0] - extent_x, point[1] - extent_y, point[0] + extent_x, point[1] + extent_y)


def footprint_box(footprint: Mapping[str, Any]) -> Box | None:
    """A PCB component's box: its courtyard, else its pads' extent."""

    courtyard = courtyard_box(footprint)
    if courtyard is not None:
        return courtyard
    pads = footprint.get("pads") or []
    return union(
        box for box in (pad_box(footprint, pad) for pad in pads if isinstance(pad, Mapping)) if box is not None
    )


def board_outline(placements: Mapping[str, Any] | None) -> Box | None:
    """The board's ``outlineBounds`` in mm, when the member carries one."""

    if not isinstance(placements, Mapping):
        return None
    bounds = placements.get("outlineBounds")
    if not isinstance(bounds, Mapping):
        return None
    minimum, maximum = _point(bounds.get("minNm")), _point(bounds.get("maxNm"))
    if minimum is None or maximum is None:
        return None
    return Box(minimum[0], minimum[1], maximum[0], maximum[1])


def placements_by_reference(placements: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    found: dict[str, Mapping[str, Any]] = {}
    for footprint in (placements or {}).get("footprints") or []:
        reference = str(footprint.get("reference") or "")
        if reference and reference not in found:
            found[reference] = footprint
    return found


def component_highlight(placements: Mapping[str, Any] | None, reference: str) -> Highlight:
    footprint = placements_by_reference(placements).get(reference)
    return Highlight("component", reference, footprint_box(footprint) if footprint else None)


def net_pcb_highlight(
    placements: Mapping[str, Any] | None,
    board_objects: Mapping[str, Any] | None,
    pins: Iterable[Mapping[str, Any]],
    net: str,
) -> Highlight:
    """A PCB net: its pads' boxes, plus any track or via point the member carries."""

    footprints = placements_by_reference(placements)
    boxes: list[Box] = []
    for pin in pins:
        footprint = footprints.get(str(pin.get("reference") or ""))
        if footprint is None:
            continue
        number = str(pin.get("pin") or "")
        pad = next(
            (item for item in footprint.get("pads") or [] if str(item.get("number") or "") == number),
            None,
        )
        if pad is not None:
            box = pad_box(footprint, pad)
            if box is not None:
                boxes.append(box)
    for document in (board_objects or {}).get("documents") or []:
        for item in document.get("objects") or []:
            if item.get("kind") not in {"track", "arc", "via"}:
                continue
            if str(item.get("net") or "") != net:
                continue
            point = _point(item.get("at"))
            if point is not None:
                # board-objects carries a centroid, not the track's endpoints.
                boxes.append(Box(point[0] - 0.1, point[1] - 0.1, point[0] + 0.1, point[1] + 0.1))
    box = union(boxes)
    return Highlight("net", net, box)


def _symbol_reference(item: Mapping[str, Any]) -> str:
    reference = str(item.get("refdes") or "")
    if reference:
        return reference
    for instance in item.get("instances") or []:
        if instance.get("reference"):
            return str(instance["reference"])
    return ""


def schematic_symbols(document: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    found: dict[str, Mapping[str, Any]] = {}
    for item in (document or {}).get("objects") or []:
        if item.get("kind") != "symbol":
            continue
        reference = _symbol_reference(item)
        if reference and reference not in found:
            found[reference] = item
    return found


def _schematic_points(
    document: Mapping[str, Any] | None,
    symbol: Mapping[str, Any] | None,
    *,
    pin: str | None = None,
    include_anchor: bool = True,
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    anchor = _point(symbol.get("at")) if symbol is not None else None
    if include_anchor and anchor is not None:
        points.append(anchor)
    parent_uuid = str(symbol.get("uuid") or "") if symbol is not None else ""
    for item in (document or {}).get("objects") or []:
        if item.get("kind") != "pin":
            continue
        if parent_uuid and str(item.get("parentUuid") or "") != parent_uuid:
            continue
        if pin is not None and str(item.get("number") or "") != pin:
            continue
        point = _point(item.get("at"))
        if point is not None:
            points.append(point)
    return points


def _box_of_points(points: Sequence[tuple[float, float]], margin: float = SCHEMATIC_MARGIN_MM) -> Box | None:
    if not points:
        return None
    return Box(
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    ).padded(margin)


def component_highlight_schematic(
    document: Mapping[str, Any] | None, reference: str
) -> Highlight:
    symbol = schematic_symbols(document).get(reference)
    if symbol is None:
        return Highlight("component", reference, None)
    return Highlight("component", reference, _box_of_points(_schematic_points(document, symbol)))


def schematic_pin_point(
    document: Mapping[str, Any] | None, reference: str, pin: str
) -> tuple[float, float] | None:
    """One schematic pin's point on the page, in mm (y down).

    ``_schematic_points`` also carries the symbol's own anchor; a pin's
    position is the pin object's ``at``, so the anchor is left out here.
    """

    symbol = schematic_symbols(document).get(reference)
    if symbol is None:
        return None
    points = _schematic_points(document, symbol, pin=pin, include_anchor=False)
    return points[0] if points else None


def index_component(index: Mapping[str, Any], reference: str) -> Mapping[str, Any] | None:
    """The semantic-index component whose reference matches, ignoring case."""

    folded = reference.casefold()
    for component in index.get("components") or []:
        if str(component.get("reference") or "").casefold() == folded:
            return component
    return None


def sheet_for_component(index: Mapping[str, Any], reference: str) -> str | None:
    """The first sheet instance path a component is drawn on, if any."""

    component = index_component(index, reference)
    if component is None:
        return None
    first = next(iter(component.get("schematicRefs") or []), None)
    return str(first.get("sheetInstancePath")) if isinstance(first, Mapping) else None


def net_highlight_schematic(
    document: Mapping[str, Any] | None,
    pins: Iterable[Mapping[str, Any]],
    net: str,
) -> Highlight:
    symbols = schematic_symbols(document)
    points: list[tuple[float, float]] = []
    for pin in pins:
        reference = str(pin.get("reference") or "")
        number = str(pin.get("pin") or "")
        symbol = symbols.get(reference)
        if symbol is None:
            continue
        points.extend(_schematic_points(document, symbol, pin=number) or _schematic_points(document, symbol))
    return Highlight("net", net, _box_of_points(points))


def document_for_path(
    board_objects: Mapping[str, Any] | None, repo_path: str
) -> Mapping[str, Any] | None:
    for document in (board_objects or {}).get("documents") or []:
        if str(document.get("path") or "") == repo_path:
            return document
    return None


def document_for_symbol(
    board_objects: Mapping[str, Any] | None, symbol_uuid: str
) -> Mapping[str, Any] | None:
    if not symbol_uuid:
        return None
    for document in (board_objects or {}).get("documents") or []:
        for item in document.get("objects") or []:
            if item.get("kind") == "symbol" and str(item.get("uuid") or "") == symbol_uuid:
                return document
    return None


__all__ = [
    "Box",
    "FIT_MARGIN",
    "Highlight",
    "MIN_REGION_MM",
    "NM_PER_MM",
    "SCHEMATIC_MARGIN_MM",
    "board_outline",
    "component_highlight",
    "component_highlight_schematic",
    "courtyard_box",
    "document_for_path",
    "document_for_symbol",
    "fit_region",
    "footprint_box",
    "index_component",
    "net_highlight_schematic",
    "net_pcb_highlight",
    "pad_box",
    "placements_by_reference",
    "rotate_local",
    "schematic_pin_point",
    "schematic_symbols",
    "sheet_for_component",
    "union",
]
