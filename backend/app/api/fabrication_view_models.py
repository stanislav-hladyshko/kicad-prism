"""Response models for the fabrication-viewer routes (ADR scripting-and-mcp §3.2).

``FabricationPackage.view`` and ``placement_service.view_of`` are pure and
already answer JSON-compatible values; these models name what they return.
``extra="allow"`` keeps a field a later parser adds on the wire.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class WireModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class FabricationLayer(WireModel):
    id: str
    name: str
    function: str
    role: str
    side: str
    colour: str
    file: str
    kind: str
    warnings: list[str]


class DrillTool(WireModel):
    diameter: float
    plated: bool
    function: str
    hits: int
    slots: int
    file: str


class DrillSummary(WireModel):
    tools: list[DrillTool]
    holes: int
    slots: int
    smallest: Optional[float]


class BoardSize(WireModel):
    width: float
    height: float


class FabricationView(WireModel):
    present: bool
    renderVersion: int
    bounds: Optional[list[float]]
    board: Optional[list[float]]
    size: Optional[BoardSize]
    copperLayers: int
    layers: list[FabricationLayer]
    drill: DrillSummary


class PlacedPart(WireModel):
    ref: str
    value: str
    package: str
    x: float
    y: float
    rotation: float
    side: str
    status: str


class MissingPart(WireModel):
    ref: str
    value: str
    package: str
    status: str


class PlacementCounts(WireModel):
    placed: int
    top: int
    bottom: int
    notInBom: int
    dnpPlaced: int
    notPlaced: int


class PlacementFiles(WireModel):
    positions: str
    bom: str


class PlacementView(WireModel):
    present: bool
    parts: list[PlacedPart]
    missing: list[MissingPart]
    counts: PlacementCounts
    hasBom: bool
    files: PlacementFiles
    warnings: list[str]


__all__ = [
    "FabricationView",
    "PlacementView",
]
