"""Semantic-index routes of /api/projects (split out of projects.py by WP-S1)."""

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api._helpers import get_project_for_role_or_404, gzip_served_path
from app.core.errors import caller_errors, value_errors_as
from app.core.security import AuthenticatedUser, require_designer, require_viewer
from app.services import semantic_index_service

router = APIRouter(dependencies=[Depends(require_viewer)])


class SemanticIndexGenerator(BaseModel):
    """The generator envelope the status route reports."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    version: str
    build: str
    cacheTag: str
    kicadMonkeyVersion: str


class SemanticIndexStatus(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # ``schema`` is the payload's own key; the field is named around Pydantic's
    # deprecated ``BaseModel.schema`` attribute and serialized by its alias.
    schema_document: str = Field(alias="schema")
    projectId: str
    sourceRevisionKey: str
    commit: Optional[str]
    available: bool
    generator: SemanticIndexGenerator


class SemanticIndexDocument(BaseModel):
    """The generated semantic-index member, served as stored.

    The document's own schema belongs to ``prism-kicad`` and the route passes
    its file through without parsing it, so this model declares only the JSON
    object the reader receives.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)


@router.get(
    "/{project_id}/semantic-index/status",
    response_model=SemanticIndexStatus,
    summary="Whether the revision's semantic index is ready",
)
async def get_semantic_index_status(
    project_id: str,
    response: Response,
    commit: Optional[str] = Query(default=None),
    user: AuthenticatedUser = Depends(require_viewer),
):
    project = get_project_for_role_or_404(project_id, user.role)
    with value_errors_as(400):
        status, headers = await asyncio.to_thread(semantic_index_service.get_status, project, commit)
    for name, value in headers.items():
        response.headers[name] = value
    return status


class SemanticIndexGenerateRequest(BaseModel):
    commit: Optional[str] = None
    force: bool = False


class SemanticIndexGenerateQueued(BaseModel):
    """The bundle build that produces the index: queued, or already reusable."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    job_id: str
    status: str


@router.post(
    "/{project_id}/semantic-index/generate",
    dependencies=[Depends(require_designer)],
    response_model=SemanticIndexGenerateQueued,
    summary="Queue the bundle build that generates the revision's semantic index",
)
async def generate_semantic_index(
    project_id: str,
    request: SemanticIndexGenerateRequest,
    user: AuthenticatedUser = Depends(require_viewer),
):
    _ = get_project_for_role_or_404(project_id, user.role)
    with caller_errors((ValueError, 400), (RuntimeError, 503), (LookupError, 404)):
        result = await asyncio.to_thread(
            semantic_index_service.start_generate_job,
            project_id,
            commit=request.commit,
            force=request.force,
            requested_by=user.email,
        )
        return result


@router.get(
    "/{project_id}/semantic-index/identity",
    response_model=SemanticIndexDocument,
    summary="The revision's semantic-index document",
)
async def get_semantic_index_identity(
    request: Request,
    project_id: str,
    commit: Optional[str] = Query(default=None),
    user: AuthenticatedUser = Depends(require_viewer),
):
    project = get_project_for_role_or_404(project_id, user.role)
    with caller_errors((ValueError, 400), (RuntimeError, 503)):
        # The stored file goes out as is: parsing and re-serializing a large
        # board's index held tens of MB per request, and the process kept them.
        artifact = await asyncio.to_thread(
            semantic_index_service.artifact_file,
            project,
            commit,
        )
        path, source_revision_key = artifact
        # ADR G9: a bundle-served file is identified by the design revision key
        # and the bundle's generator digest.
        etag = f'"{source_revision_key}-{artifact.generator}"'
        headers = {"Cache-Control": "private, max-age=300", "ETag": etag, **artifact.headers}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        served = await asyncio.to_thread(
            gzip_served_path, request, path, "application/json", f"{source_revision_key}-{artifact.generator}", headers
        )
        return FileResponse(served, media_type="application/json", headers=headers)
