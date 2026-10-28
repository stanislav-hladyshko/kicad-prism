"""Response models for the remote-provider routes (ADR scripting-and-mcp §3.2).

The payload shapes are the desktop KiCad placement contract (``remote_head_payload``
and ``CatalogPlacement.build_manifest`` / ``build_inline_bundle``). Nested
library payloads stay ``dict``/``Any`` because they are KiCad's own structures;
every top-level key and primitive the route answers is named here.
``extra="allow"`` keeps a key this contract adds later on the wire.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class WireModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class ReleasedRevision(WireModel):
    revision_id: str
    version: int
    released_at: str


class ComponentPayload(WireModel):
    """One released component as ``_component_payload`` shapes it for KiCad."""

    id: str
    slug: str
    name: str
    identity_kind: str
    manufacturer: str
    mpn: str
    description: str
    package_name: str
    category: str
    datasheet_url: str
    summary: str
    version: str
    library_name: str
    symbol_name: str
    representations: list[dict[str, Any]]
    default_representation_id: str
    effective_representation_id: str
    assets: list[dict[str, Any]]
    availability_state: str
    missing_assets: list[str]
    place_enabled: bool
    release_status: str
    workflow_stage: str
    supply: dict[str, Any]
    preview_status: dict[str, Any]
    symbol_preview_url: str
    footprint_preview_url: str
    revision_id: str
    manifest_url: str
    inline_url: str


class ComponentDetail(ComponentPayload):
    released_revisions: list[ReleasedRevision]


class ComponentPage(WireModel):
    items: list[ComponentPayload]
    total: Optional[int]
    has_more: bool
    page: int
    pages: Optional[int]
    page_size: int


class CategoryCount(WireModel):
    name: str
    count: int


class CategoryList(WireModel):
    categories: list[CategoryCount]


class ManifestAsset(WireModel):
    asset_type: str
    name: str
    target_library: str
    target_name: str
    content_type: str
    size_bytes: int
    sha256: str
    required: bool
    download_url: str


class PartManifest(WireModel):
    part_id: str
    display_name: str
    summary: str
    license: str
    representation_id: str
    library_name: str
    symbol_name: str
    assets: list[ManifestAsset]


class InlineBundle(WireModel):
    part_id: str
    display_name: str
    representation_id: str
    library: str
    symbol_name: str
    compression: str
    data: str


__all__ = [
    "CategoryList",
    "ComponentDetail",
    "ComponentPage",
    "InlineBundle",
    "PartManifest",
]
