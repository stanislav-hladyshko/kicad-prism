"""PNG renders of a project revision (ADR D9, §3.7).

``POST .../renders`` starts a worker render or answers an already stored one.
``GET .../renders/{id}`` reports its record, and ``GET .../renders/{id}.png``
serves the PNG as an immutable derived asset. The renderer, its cache and its
``render_view`` job live in ``app.services.design_render.service``.
"""

from __future__ import annotations

import asyncio
from typing import Any, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api._helpers import PINNED_CACHE_CONTROL, get_project_for_role_or_404
from app.core.errors import BadRequest
from app.core.security import AuthenticatedUser, require_viewer
from app.services.design_render import service as renders

router = APIRouter(dependencies=[Depends(require_viewer)])

_HEX64 = frozenset("0123456789abcdef")


class RenderRegion(BaseModel):
    x: float = Field(..., description="Left edge in mm in the view's frame")
    y: float = Field(..., description="Top edge in mm in the view's frame")
    width: float = Field(..., gt=0, description="Region width in mm")
    height: float = Field(..., gt=0, description="Region height in mm")


class RenderHighlight(BaseModel):
    components: List[str] = Field(default_factory=list, description="Reference designators")
    nets: List[str] = Field(default_factory=list, description="Net names")


class RenderRequest(BaseModel):
    view: Literal["schematic", "pcb", "3d"]
    revision: Optional[str] = Field(
        default=None,
        description="A full commit sha, branch or tag; omitted reads the checkout's HEAD.",
    )
    sheet: Optional[str] = Field(
        default=None,
        description="Schematic sheet instance path or page number; default root or the first highlight's sheet.",
    )
    layers: Literal["front", "back", "copper", "all"] = "front"
    side: Literal["top", "bottom"] = "top"
    highlight: Optional[RenderHighlight] = None
    region: Optional[RenderRegion] = None
    width: int = Field(default=1200, ge=renders.plots.MIN_WIDTH, le=renders.plots.MAX_WIDTH)


class RenderHighlightOut(BaseModel):
    kind: Literal["component", "net"]
    key: str
    found: bool


class RenderStartResponse(BaseModel):
    renderId: str
    jobId: Optional[str] = None
    status: str


class RenderRecordResponse(BaseModel):
    renderId: str
    status: str
    view: str
    commit: str
    sheet: Optional[str] = None
    region: Optional[dict[str, float]] = None
    width: int
    height: Optional[int] = None
    highlights: List[RenderHighlightOut] = Field(default_factory=list)
    pngUrl: Optional[str] = None


@router.post(
    "/{project_id}/renders",
    response_model=RenderStartResponse,
    response_model_exclude_none=True,
    summary="Queue a PNG render of a revision, or reuse a stored one",
)
async def start_render(
    project_id: str,
    request: RenderRequest,
    user: AuthenticatedUser = Depends(require_viewer),
):
    project = get_project_for_role_or_404(project_id, user.role)
    try:
        return await asyncio.to_thread(
            renders.start_render,
            project,
            request.model_dump(exclude_none=True),
            requested_by=user.email,
        )
    except renders.RenderRequestError:
        raise
    except ValueError as error:
        # The only other refusal: ``design_query.revision`` cannot resolve it.
        raise BadRequest("Unknown revision", code="revision-unknown") from error


@router.get(
    "/{project_id}/renders/{render_id}.png",
    response_class=FileResponse,
    responses={200: {"content": {"image/png": {}}, "description": "The rendered PNG"}},
    summary="Download a rendered PNG",
)
async def render_png(
    project_id: str,
    render_id: str,
    user: AuthenticatedUser = Depends(require_viewer),
):
    if len(render_id) != 64 or any(character not in _HEX64 for character in render_id):
        raise HTTPException(status_code=404, detail="Render not found")
    project = get_project_for_role_or_404(project_id, user.role)
    path = await asyncio.to_thread(renders.png_path, project, render_id)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Render not found")
    return FileResponse(
        path,
        media_type="image/png",
        headers={
            "ETag": f'"{render_id}"',
            "Cache-Control": PINNED_CACHE_CONTROL,
        },
    )


@router.get(
    "/{project_id}/renders/{render_id}",
    response_model=RenderRecordResponse,
    response_model_exclude_none=True,
    summary="Read a render's record",
)
async def render_record(
    project_id: str,
    render_id: str,
    user: AuthenticatedUser = Depends(require_viewer),
):
    if len(render_id) != 64 or any(character not in _HEX64 for character in render_id):
        raise HTTPException(status_code=404, detail="Render not found")
    project = get_project_for_role_or_404(project_id, user.role)
    record: Optional[dict[str, Any]] = await asyncio.to_thread(renders.get_render, project, render_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Render not found")
    return record
