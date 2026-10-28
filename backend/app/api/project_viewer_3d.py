"""WebGPU 3D routes of /api/projects (split out of projects.py by WP-S1).

The routes serve the analytic model from the design bundle
(``viewer_3d_bundle.py``, ADR amendment V2): status and generate go through the
bundle and its ``design_bundle`` job, ``/webgpu-3d/analytic`` is the viewer's
bundle document, and the asset route serves its members under
``design-bundle/``. The mesh path these routes once also served is gone (System
Builder v2, stage 0).
"""

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from app.api import project_viewer_3d_models as models
from app.api._helpers import get_project_for_role_or_404
from app.core.errors import caller_errors, value_errors_as
from app.core.security import AuthenticatedUser, require_designer, require_viewer
from app.services import viewer_3d_bundle
from app.services.design_bundle import regenerate
from app.services.design_bundle import jobs as bundle_jobs
from app.services.design_bundle.cli import CliUnavailable

router = APIRouter(dependencies=[Depends(require_viewer)])


def _bundle_errors():
    """A missing project or ``prism-kicad`` as the caller's status (design_bundles.py maps them so)."""

    return caller_errors((LookupError, 404), (CliUnavailable, 503))


@router.get(
    "/{project_id}/webgpu-3d/status",
    response_model=models.WebGpu3dStatus,
    response_model_exclude_unset=True,
    summary="The 3D tab's status for a revision, queueing its design bundle",
)
async def get_webgpu_3d_status(
    project_id: str,
    commit: Optional[str] = Query(default=None),
    user: AuthenticatedUser = Depends(require_viewer),
):
    _ = get_project_for_role_or_404(project_id, user.role)
    with value_errors_as(400), _bundle_errors():
        return await asyncio.to_thread(viewer_3d_bundle.status, project_id, commit, requested_by=user.email)


class WebGpu3dGenerateRequest(BaseModel):
    commit: Optional[str] = None
    force: bool = False


@router.post(
    "/{project_id}/webgpu-3d/generate",
    response_model=models.WebGpu3dGenerated,
    dependencies=[Depends(require_designer)],
    summary="Queue the revision's design bundle, optionally rebuilding it",
)
async def generate_webgpu_3d(
    project_id: str,
    request: WebGpu3dGenerateRequest,
    user: AuthenticatedUser = Depends(require_viewer),
):
    _ = get_project_for_role_or_404(project_id, user.role)
    # The bundle services refuse with exceptions written for the caller (as design_bundles.py maps them).
    with caller_errors(
        (regenerate.RegenerateConflict, 409),
        (regenerate.RegenerateNotApplicable, 422),
        (LookupError, 404),
        (CliUnavailable, 503),
    ):
        return await asyncio.to_thread(_generate_bundle, project_id, request, user.email)


def _generate_bundle(project_id: str, request: WebGpu3dGenerateRequest, requested_by: str) -> dict:
    """Queue the commit's design bundle; ``force`` rebuilds it without reuse (K2b)."""

    if not request.force:
        return {
            "job_id": bundle_jobs.start_design_bundle_job(
                project_id, commit=request.commit, requested_by=requested_by, retry_failed=True
            )
        }
    return {"job_id": regenerate.regenerate(project_id, commit=request.commit, requested_by=requested_by).job_id}


@router.get(
    "/{project_id}/webgpu-3d/analytic",
    response_model=models.WebGpu3dAnalytic,
    summary="The viewer's analytic bundle document for a revision",
)
async def get_webgpu_3d_analytic(
    project_id: str,
    commit: Optional[str] = Query(default=None),
    user: AuthenticatedUser = Depends(require_viewer),
):
    """The viewer's ``prism.viewer_3d.analytic.a0`` document."""

    _ = get_project_for_role_or_404(project_id, user.role)
    with value_errors_as(400), _bundle_errors():
        document = await asyncio.to_thread(viewer_3d_bundle.document, project_id, commit)
    if document is None:
        raise HTTPException(status_code=404, detail="The board's analytic model is not stored yet")
    # Staged: the parts join when stage models stores them, so no long caching.
    return JSONResponse(document, headers={"Cache-Control": "no-store"})


@router.get(
    "/{project_id}/webgpu-3d/assets/{source_revision_key}/{generator_build}/{asset_path:path}",
    response_class=Response,
    summary="Serve one design-bundle member by revision key and generator",
    responses={
        200: {
            "content": {
                "application/octet-stream": {},
                "application/json": {},
                "model/gltf-binary": {},
            },
            "description": "The member's stored bytes.",
        }
    },
)
async def get_webgpu_3d_asset(
    project_id: str,
    source_revision_key: str,
    generator_build: str,
    asset_path: str,
    user: AuthenticatedUser = Depends(require_viewer),
):
    _ = get_project_for_role_or_404(project_id, user.role)
    if not asset_path.startswith(viewer_3d_bundle.ASSET_PREFIX):
        raise HTTPException(status_code=404, detail="WebGPU asset not found")
    with value_errors_as(400), _bundle_errors():
        found = await asyncio.to_thread(
            viewer_3d_bundle.member,
            project_id,
            source_revision_key,
            generator_build,
            asset_path[len(viewer_3d_bundle.ASSET_PREFIX):],
        )
    if found is None:
        raise HTTPException(status_code=404, detail="WebGPU asset not found")
    data, media_type = found
    # A revision key and generator name these bytes for good.
    return Response(content=data, media_type=media_type, headers={"Cache-Control": "private, max-age=31536000, immutable"})
