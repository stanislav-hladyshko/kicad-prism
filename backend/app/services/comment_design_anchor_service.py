"""Resolve a comment anchor from design identity (ADR scripting-and-mcp §10 D16).

An agent names the thing it is reviewing — a component reference, a net name, a
schematic sheet or a comparison change — and the server turns that into the same
``context``/``location``/element fields the browser sends when a person clicks
the item. Nothing here invents a coordinate: the revision's design bundle
supplies the geometry ``render_view`` already draws with.

The coordinate frame is the viewer's, and the frontend's comment click builds
``location`` the same way:

* the ecad viewer reports a semantic selection whose ``x``/``y`` are the centre
  of the selected item's bounding box, in millimetres on the board frame (PCB)
  or the page (schematic), with ``y`` growing down;
* ``design_render.geometry`` returns boxes in exactly that frame, so a
  component's anchor is its courtyard box centre and a net's is its first pad's
  centre. A schematic sheet has no geometry of its own, so its anchor is the
  page origin.

A missing bundle is a ``409 bundle-not-ready``: a comment cannot be anchored to
geometry that does not exist yet. An unknown element is ``404 anchor-not-found``
and a malformed selection is ``400 invalid-anchor``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from app.core.errors import BadRequest, Conflict, NotFound
from app.services.design_bundle import consumer
from app.services.design_bundle.errors import DesignDataFailed, DesignDataPending
from app.services.design_query import index as design_index
from app.services.design_render import geometry

#: The one consumer name this read uses; ``bundle_read`` records it for the build.
CONSUMER = "COMMENT_ANCHOR"

SEMANTIC_INDEX = "projections/semantic-index.json"
PLACEMENTS = "projections/placements.json"
BOARD_OBJECTS = "projections/board-objects.json"

ANCHOR_KINDS = ("component", "net", "sheet")
VIEWS = ("pcb", "sch")
#: A sheet only exists in the schematic; component and net anchors default to PCB.
DEFAULT_VIEW = {"component": "pcb", "net": "pcb", "sheet": "sch"}


class AnchorNotFound(NotFound):
    """The named component, net or sheet is not in the revision."""

    code = "anchor-not-found"


class InvalidAnchor(BadRequest):
    """The request names no anchor, several, or a view the anchor cannot use."""

    code = "invalid-anchor"


class BundleNotReady(Conflict):
    """The revision's design bundle is not built yet, or could not be opened."""

    code = "bundle-not-ready"


@dataclass(frozen=True)
class DesignAnchor:
    """The browser-shaped anchor a design-identity comment resolves to."""

    context: str
    x: float
    y: float
    layer: str
    page: str
    element_id: Optional[str]
    element_ref: Optional[str]
    element_type: Optional[str]

    def location(self) -> dict[str, Any]:
        return {
            "x": round(self.x, 6),
            "y": round(self.y, 6),
            "layer": self.layer,
            "page": self.page,
        }


def _member(read: Any, path: str) -> Any:
    return read.json(path) if read.ready(path) else None


def _open_bundle(project_id: str, commit: str, requested_by: str) -> Any:
    """The three projection members this resolver reads, or ``bundle-not-ready``.

    ``bundle_read`` raises ``DesignDataPending`` while the build runs and
    ``DesignDataFailed`` when it ended badly; both mean there is no geometry to
    anchor to yet. A project or revision the reader cannot open is the same
    answer to a caller.
    """

    try:
        return consumer.bundle_read(
            CONSUMER,
            project_id,
            commit,
            [SEMANTIC_INDEX, PLACEMENTS, BOARD_OBJECTS],
            requested_by=requested_by,
        )
    except (DesignDataPending, DesignDataFailed, LookupError) as error:
        raise BundleNotReady(
            "The design bundle for this revision is not ready; try again when it is built."
        ) from error


def _selected(component: str, net: str, sheet: str) -> tuple[str, str]:
    """The one anchor kind and value the request named."""

    values = {"component": component, "net": net, "sheet": sheet}
    present = [(kind, value.strip()) for kind, value in values.items() if value and value.strip()]
    if len(present) != 1:
        raise InvalidAnchor("Name exactly one of component, net or sheet.")
    return present[0]


def _view(anchor_kind: str, requested: str) -> str:
    view = (requested or DEFAULT_VIEW[anchor_kind]).strip().lower()
    if view not in VIEWS:
        raise InvalidAnchor("view must be 'pcb' or 'sch'.")
    if anchor_kind == "sheet" and view != "sch":
        raise InvalidAnchor("A schematic sheet anchor is always on the schematic (view 'sch').")
    return view


def _pcb_layer(footprint: Mapping[str, Any] | None) -> str:
    return "B.Cu" if str((footprint or {}).get("side") or "") == "back" else "F.Cu"


def _footprint(
    placements: Mapping[str, Any] | None, reference: str, footprint_uuid: Optional[str]
) -> Mapping[str, Any] | None:
    by_reference = geometry.placements_by_reference(placements)
    if footprint_uuid:
        for footprint in (placements or {}).get("footprints") or []:
            if str(footprint.get("uuid") or "") == footprint_uuid:
                return footprint
    return by_reference.get(reference)


def _terminal(index: Mapping[str, Any], reference: str, pin: str) -> Mapping[str, Any] | None:
    folded = reference.casefold()
    for terminal in index.get("terminals") or []:
        if (
            str(terminal.get("reference") or "").casefold() == folded
            and str(terminal.get("pin") or "") == pin
        ):
            return terminal
    return None


def _component_anchor(
    reference: str,
    view: str,
    index: Mapping[str, Any],
    placements: Mapping[str, Any] | None,
    board_objects: Mapping[str, Any] | None,
) -> DesignAnchor:
    component = geometry.index_component(index, reference)
    if component is None:
        raise AnchorNotFound(f"Component {reference!r} was not found in this revision.")

    if view == "pcb":
        pcb_refs = component.get("pcbRefs") or []
        footprint_uuid = (
            str(pcb_refs[0].get("footprintUuid") or "") if pcb_refs and isinstance(pcb_refs[0], Mapping) else ""
        )
        footprint = _footprint(placements, reference, footprint_uuid or None)
        box = geometry.footprint_box(footprint) if footprint is not None else None
        if box is None:
            raise AnchorNotFound(f"Component {reference!r} has no footprint geometry in this revision.")
        x, y = box.center
        return DesignAnchor(
            context="PCB",
            x=x,
            y=y,
            layer=_pcb_layer(footprint),
            page="",
            element_id=footprint_uuid or str((footprint or {}).get("uuid") or "") or None,
            element_ref=str(component.get("reference") or reference),
            element_type="footprint",
        )

    sheet_refs = component.get("schematicRefs") or []
    sheet_ref = sheet_refs[0] if sheet_refs and isinstance(sheet_refs[0], Mapping) else None
    symbol_uuid = str((sheet_ref or {}).get("symbolUuid") or "")
    document = geometry.document_for_symbol(board_objects, symbol_uuid)
    box = geometry.component_highlight_schematic(document, reference).box
    if box is None:
        raise AnchorNotFound(f"Component {reference!r} has no schematic symbol in this revision.")
    x, y = box.center
    return DesignAnchor(
        context="SCH",
        x=x,
        y=y,
        layer="",
        page=str((sheet_ref or {}).get("page") or ""),
        element_id=symbol_uuid or None,
        element_ref=str(component.get("reference") or reference),
        element_type="symbol",
    )


def _net_anchor(
    name: str,
    view: str,
    index: Mapping[str, Any],
    placements: Mapping[str, Any] | None,
    board_objects: Mapping[str, Any] | None,
) -> DesignAnchor:
    resolved = design_index.net(dict(index), name)
    if resolved is None:
        raise AnchorNotFound(f"Net {name!r} was not found in this revision.")
    net_name = str(resolved.get("name") or name)

    if view == "pcb":
        footprints = geometry.placements_by_reference(placements)
        for pin in resolved["pins"]:
            footprint = footprints.get(str(pin.get("reference") or ""))
            if footprint is None:
                continue
            pad = next(
                (
                    item
                    for item in footprint.get("pads") or []
                    if str(item.get("number") or "") == str(pin.get("pin") or "")
                ),
                None,
            )
            box = geometry.pad_box(footprint, pad) if isinstance(pad, Mapping) else None
            if box is None:
                continue
            terminal = _terminal(index, str(pin.get("reference") or ""), str(pin.get("pin") or ""))
            x, y = box.center
            return DesignAnchor(
                context="PCB",
                x=x,
                y=y,
                layer=_pcb_layer(footprint),
                page="",
                element_id=str((terminal or {}).get("pcbPadUuid") or "") or str(footprint.get("uuid") or "") or None,
                element_ref=net_name,
                element_type="net",
            )
        # A net with no pads (a track-only net) still has a board position.
        box = geometry.net_pcb_highlight(placements, board_objects, resolved["pins"], net_name).box
        if box is None:
            raise AnchorNotFound(f"Net {name!r} has no board geometry in this revision.")
        x, y = box.center
        return DesignAnchor(
            context="PCB", x=x, y=y, layer="", page="",
            element_id=str(resolved.get("netUid") or "") or None,
            element_ref=net_name, element_type="net",
        )

    for pin in resolved["pins"]:
        reference = str(pin.get("reference") or "")
        number = str(pin.get("pin") or "")
        component = geometry.index_component(index, reference)
        if component is None:
            continue
        for sheet_ref in component.get("schematicRefs") or []:
            if not isinstance(sheet_ref, Mapping):
                continue
            document = geometry.document_for_symbol(
                board_objects, str(sheet_ref.get("symbolUuid") or "")
            )
            point = geometry.schematic_pin_point(document, reference, number)
            if point is None:
                continue
            terminal = _terminal(index, reference, number)
            return DesignAnchor(
                context="SCH",
                x=point[0],
                y=point[1],
                layer="",
                page=str(sheet_ref.get("page") or ""),
                element_id=(
                    str((terminal or {}).get("schematicPinUuid") or "")
                    or str(sheet_ref.get("symbolUuid") or "")
                    or None
                ),
                element_ref=net_name,
                element_type="net",
            )
    raise AnchorNotFound(f"Net {name!r} has no schematic geometry in this revision.")


def _sheet_anchor(name: str, index: Mapping[str, Any]) -> DesignAnchor:
    wanted = name.strip()
    matches: list[Mapping[str, Any]] = []
    for sheet in index.get("sheetInstances") or []:
        if not isinstance(sheet, Mapping):
            continue
        candidates = (
            sheet.get("sheetInstancePath"),
            sheet.get("sheetPath"),
            sheet.get("page"),
            sheet.get("sheetName"),
        )
        if wanted in {str(value) for value in candidates if value}:
            matches.append(sheet)
    if not matches:
        raise AnchorNotFound(f"Schematic sheet {name!r} was not found in this revision.")
    if len(matches) > 1:
        raise InvalidAnchor(
            "That sheet name matches more than one sheet; use its sheet instance path."
        )
    sheet = matches[0]
    page = str(sheet.get("sheetInstancePath") or sheet.get("sheetPath") or "")
    # A sheet has no symbol on its own page, so the comment is a world marker at
    # that page's origin; setting elementId would ask the viewer to resolve a
    # source item that belongs to the parent page instead.
    return DesignAnchor(
        context="SCH",
        x=0.0,
        y=0.0,
        layer="",
        page=page,
        element_id=None,
        element_ref=page or name.strip(),
        element_type="sheet",
    )


def resolve_anchor(
    project: Any,
    *,
    commit: str,
    component: str = "",
    net: str = "",
    sheet: str = "",
    view: str = "",
    requested_by: str = "",
) -> DesignAnchor:
    """The revision's geometry for the one design element the request named.

    ``commit`` is already resolved (``design_query.revision.resolve``), so an
    unknown ref never reaches this function.
    """

    kind, value = _selected(component, net, sheet)
    chosen_view = _view(kind, view)
    read = _open_bundle(str(project.id), commit, requested_by)
    index = _member(read, SEMANTIC_INDEX) or {}
    placements = _member(read, PLACEMENTS)
    board_objects = _member(read, BOARD_OBJECTS)

    if kind == "component":
        return _component_anchor(value, chosen_view, index, placements, board_objects)
    if kind == "net":
        return _net_anchor(value, chosen_view, index, placements, board_objects)
    return _sheet_anchor(value, index)


__all__ = [
    "ANCHOR_KINDS",
    "AnchorNotFound",
    "BundleNotReady",
    "CONSUMER",
    "DesignAnchor",
    "InvalidAnchor",
    "resolve_anchor",
]
