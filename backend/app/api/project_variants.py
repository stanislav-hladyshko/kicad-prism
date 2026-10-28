"""Design-variant catalog endpoint (VAR-08).

``GET /api/projects/{project_id}/variants`` returns the revision's variant
catalog (contract packet v1.0 section 3.1). The project lookup is role-aware,
the source reads run off the event loop, and the response carries an ETag so a
client can revalidate instead of refetching.

Responses are private and revalidate on every request: the revision's bundle
content and the catalog generator's behavior can both change without the URL
changing. ETags avoid resending an unchanged payload. The revision is opened
once; the shaped catalog is cached by bundle key and member digest, so a
revalidation does not inflate and parse the multi-megabyte semantic index.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.api._helpers import get_project_for_role_or_404
from app.core.security import AuthenticatedUser, require_viewer
from app.services import variant_catalog_service
from app.services.design_bundle import consumer

router = APIRouter(dependencies=[Depends(require_viewer)])


class VariantCatalogEntry(BaseModel):
    """One named design variant in the catalog order."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    description: Optional[str]
    sources: list[str]


class VariantCatalogDiagnostic(BaseModel):
    """A catalog-builder diagnostic, in today's key order."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    code: Optional[str] = None
    severity: Optional[str] = None
    source: Optional[str] = None
    path: Optional[str] = None
    message: Optional[str] = None
    detail: Optional[Any] = None


class ProjectVariantCatalog(BaseModel):
    """The revision's variant catalog and its diagnostics."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # ``schema`` is the payload's own key; the field is named around Pydantic's
    # deprecated ``BaseModel.schema`` attribute and serialized by its alias.
    schema_document: str = Field(alias="schema")
    projectId: str
    commit: Optional[str]
    sourceRevisionKey: str
    variants: list[VariantCatalogEntry]
    diagnostics: list[VariantCatalogDiagnostic]



def _catalog_generator_tag() -> str:
    """Identify the code that shapes catalog output, for the ETag.

    The source revision key covers the input revision; this covers the
    discovery code. Both must move for a cached entry to be reusable, so the
    tag hashes the module whose logic can change the answer.
    """

    digest = hashlib.sha256()
    digest.update(Path(variant_catalog_service.__file__).read_bytes())
    return f"{variant_catalog_service.SCHEMA}-{digest.hexdigest()[:12]}"


CATALOG_GENERATOR_TAG = _catalog_generator_tag()


@router.get(
    "/{project_id}/variants",
    response_model=ProjectVariantCatalog,
    summary="The revision's ordered design-variant catalog",
)
async def get_project_variants(
    project_id: str,
    request: Request,
    commit: Optional[str] = Query(default=None),
    user: AuthenticatedUser = Depends(require_viewer),
):
    project = get_project_for_role_or_404(project_id, user.role)
    try:
        read = await asyncio.to_thread(
            consumer.bundle_read,
            "SEMANTIC_INDEX",
            project_id,
            commit,
            [variant_catalog_service.SEMANTIC_INDEX_MEMBER],
        )
        payload = await asyncio.to_thread(
            variant_catalog_service.discover_variant_catalog,
            project,
            commit,
            read,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Could not read design variants for this revision") from error
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise HTTPException(status_code=503, detail="Could not read design variants for this revision") from error

    identity = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
    etag = f'"{identity}-{CATALOG_GENERATOR_TAG}"'
    # Source commits are immutable, but the generator can change at deployment.
    # Revalidate every response; cached discovery and ETags keep this cheap.
    headers = {"Cache-Control": "private, no-cache", "ETag": etag, **read.response_headers()}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(
        content=json.dumps(payload),
        media_type="application/json",
        headers=headers,
    )
