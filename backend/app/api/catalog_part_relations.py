"""Catalog "mates with" and 3D model alignment.

Routes under ``/api/catalog`` beside ``catalog_admin``, served by the catalog's
``part_relations`` facade.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import caller_errors
from app.core.security import AuthenticatedUser, require_catalog_browser, require_catalog_writer
from app.services.catalog import models as catalog_models
from app.services.catalog_job_service import catalog_jobs
from app.services.component_catalog_service import catalog_service

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


class MateOut(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    componentId: str
    name: str
    mpn: str
    manufacturer: str
    createdBy: str
    createdAt: str


class MatePage(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    items: list[MateOut]


class ModelGlb(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    key: str
    converter: str
    bounds: dict[str, Any]
    materials: int
    sizeBytes: int


class ModelAlignment(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    offsetMm: list[float]
    rotationDeg: list[float]
    scale: float
    updatedBy: str | None
    updatedAt: str | None


class ModelOut(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    assetId: str
    name: str
    stepSha256: str
    glb: ModelGlb | None
    alignment: ModelAlignment


class ModelPage(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    items: list[ModelOut]


class MateRequest(BaseModel):
    componentId: str = Field(min_length=1, max_length=200)


class ModelConversionJob(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    jobId: str | None
    status: str | None


def _mates_call(action):
    with caller_errors((LookupError, 404), (ValueError, 422)):
        return {"items": action()}


@router.get("/components/{component_id}/mates-with", response_model=MatePage,
            response_model_exclude_unset=True, summary="List the parts this part mates with")
def list_mates_with(component_id: str, user: AuthenticatedUser = Depends(require_catalog_browser)):
    """The parts this part mates with."""
    return _mates_call(lambda: catalog_service.part_relations.list_mates_with(component_id))


@router.post("/components/{component_id}/mates-with", response_model=MatePage,
             response_model_exclude_unset=True, summary="Mark two parts as mating")
def add_mate(component_id: str, body: MateRequest, user: AuthenticatedUser = Depends(require_catalog_writer)):
    return _mates_call(lambda: catalog_service.part_relations.set_mate(component_id, body.componentId, mates=True, actor=user.email))


@router.delete("/components/{component_id}/mates-with/{other_id}", response_model=MatePage,
               response_model_exclude_unset=True, summary="Remove a mating between two parts")
def remove_mate(component_id: str, other_id: str, user: AuthenticatedUser = Depends(require_catalog_writer)):
    return _mates_call(lambda: catalog_service.part_relations.set_mate(component_id, other_id, mates=False, actor=user.email))


class AlignmentRequest(BaseModel):
    offsetMm: list[float] = Field(min_length=3, max_length=3)
    rotationDeg: list[float] = Field(min_length=3, max_length=3)
    scale: float = Field(default=1.0, gt=0, le=100)


def _triple(text: str | None, name: str) -> list[float] | None:
    if text is None:
        return None
    try:
        values = [float(part) for part in text.split(",")]
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{name} takes three comma-separated numbers") from None
    if len(values) != 3:
        raise HTTPException(status_code=422, detail=f"{name} takes three comma-separated numbers")
    return values


def _require_revision(component_id: str, revision_id: str | None) -> None:
    """404 unless ``revisionId`` (when given) is a revision of that component."""
    if revision_id and not catalog_service.get_component_revision(component_id, revision_id):
        raise HTTPException(status_code=404, detail="Component revision not found")


@router.get("/components/{component_id}/models", response_model=ModelPage,
            response_model_exclude_unset=True, summary="List a part's STEP models")
def list_models(component_id: str, revision_id: str | None = Query(None, alias="revisionId"),
                user: AuthenticatedUser = Depends(require_catalog_browser)):
    """STEP models with their GLB and alignment, of the current revision or of ``revisionId`` (a
    pinned one)."""
    _require_revision(component_id, revision_id)
    return _mates_call(lambda: catalog_service.part_relations.list_models(component_id, revision_id))


@router.post("/components/{component_id}/models/convert", response_model=ModelConversionJob,
             response_model_exclude_unset=True, summary="Queue STEP to GLB conversion")
def convert_models(component_id: str, revision_id: str | None = Query(None, alias="revisionId"),
                   user: AuthenticatedUser = Depends(require_catalog_writer)):
    """Queue GLB conversion of the current revision's STEP models, or of ``revisionId``'s (a pinned one)."""
    _mates_call(lambda: catalog_service.part_relations.list_models(component_id))  # 404/422 before queueing
    _require_revision(component_id, revision_id)
    payload = {"componentId": component_id, **({"revisionId": revision_id} if revision_id else {})}
    job = catalog_jobs.enqueue("catalog_model_glb", payload, created_by=user.email,
                               idempotency_key=catalog_models.conversion_job_key(component_id, revision_id))
    return {"jobId": job.get("id"), "status": job.get("status")}


@router.put("/components/{component_id}/models/{asset_id}/alignment", response_model=ModelPage,
            response_model_exclude_unset=True, summary="Save a model's alignment")
def set_model_alignment(component_id: str, asset_id: str, body: AlignmentRequest,
                        user: AuthenticatedUser = Depends(require_catalog_writer)):
    return _mates_call(lambda: catalog_service.part_relations.set_model_alignment(component_id, asset_id, body.model_dump(),
                                                                   actor=user.email))


@router.get("/components/{component_id}/models/{asset_id}/preview.svg", response_class=Response,
            summary="Render a model preview",
            responses={200: {"content": {"image/svg+xml": {}}, "description": "An orthographic model SVG"}})
def model_preview(
    component_id: str, asset_id: str,
    view: str = Query(default="front", pattern="^(front|side|top)$"),
    offset: str | None = Query(default=None, max_length=100), rotation: str | None = Query(default=None, max_length=100),
    scale: float | None = Query(default=None, gt=0, le=100), partner: str | None = Query(default=None, max_length=200),
    user: AuthenticatedUser = Depends(require_catalog_browser),
):
    """An orthographic SVG of the model under a (possibly unsaved) alignment, optionally mated to ``partner``."""
    alignment = None
    if offset is not None or rotation is not None or scale is not None:
        alignment = {"offsetMm": _triple(offset, "offset") or [0.0, 0.0, 0.0],
                     "rotationDeg": _triple(rotation, "rotation") or [0.0, 0.0, 0.0], "scale": scale or 1.0}
    with caller_errors((LookupError, 404), (ValueError, 422)):
        svg = catalog_service.part_relations.model_preview(component_id, asset_id, view=view, alignment=alignment, partner_id=partner)
    return Response(content=svg, media_type="image/svg+xml", headers={"Cache-Control": "private, max-age=60"})


@router.get("/models/{key}.glb", response_class=FileResponse, summary="Read a converted model GLB",
            responses={200: {"content": {"model/gltf-binary": {}}, "description": "The converted GLB"}})
def model_glb(key: str, user: AuthenticatedUser = Depends(require_catalog_browser)):
    if not key.isalnum() or len(key) != 64:
        raise HTTPException(status_code=404, detail="Model not found")
    path = catalog_service.part_relations.model_glb_path(key)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Model not found")
    # Content-addressed: the key names the STEP and the converter, so the bytes never change.
    return FileResponse(path, media_type="model/gltf-binary", headers={"Cache-Control": "private, max-age=31536000, immutable"})
