"""Design-query routes of /api/projects (ADR scripting-and-mcp §3.3, §3.8).

Three reads over a revision's semantic index: search, one component, one net.
Every answer names the resolved ``commit`` and the bundle's ``designKey``, so a
caller can reproduce and cite it. The revision defaults to the checkout's
``HEAD``; a full commit id is immutable and is served with the pinned cache
header, any other revision revalidates.

The project lookup is role-aware and the index read runs off the event loop.
Index parsing is cached by bundle revision key and generator, so a search
followed by a component read parses the member once.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from app.api._helpers import PINNED_CACHE_CONTROL, get_project_for_role_or_404
from app.core.errors import BadRequest, NotFound
from app.core.security import AuthenticatedUser, require_viewer
from app.services.design_query import index as design_index
from app.services.design_query import revision as design_revision

router = APIRouter(dependencies=[Depends(require_viewer)])

_DEFAULT_KINDS: List[Literal["component", "net"]] = ["component", "net"]


class DesignRevisionOut(BaseModel):
    commit: str
    designKey: Optional[str] = None


class DesignSearchHit(BaseModel):
    kind: Literal["component", "net"]
    reference: Optional[str] = None
    name: Optional[str] = None
    value: Optional[str] = None
    footprint: Optional[str] = None
    matched: Literal["reference", "value", "field", "name"]
    field: Optional[str] = None


class DesignSearchOut(BaseModel):
    revision: DesignRevisionOut
    query: str
    results: List[DesignSearchHit]


class DesignPinOut(BaseModel):
    pin: str
    pinName: str = ""
    net: Optional[str] = None
    netUid: Optional[str] = None


class DesignAssemblyOut(BaseModel):
    dnp: Optional[bool] = None
    excludeFromBom: Optional[bool] = None
    excludeFromBoard: Optional[bool] = None
    excludeFromSim: Optional[bool] = None
    excludeFromPosFiles: Optional[bool] = None


class DesignComponent(BaseModel):
    reference: str
    componentUid: Optional[str] = None
    value: str = ""
    footprint: str = ""
    libraryRef: Optional[str] = None
    sheet: Optional[str] = None
    page: Optional[str] = None
    dnp: bool = False
    inBom: Optional[bool] = None
    assembly: DesignAssemblyOut = Field(default_factory=DesignAssemblyOut)
    fields: Dict[str, str] = Field(default_factory=dict)
    pins: List[DesignPinOut] = Field(default_factory=list)


class DesignComponentOut(BaseModel):
    revision: DesignRevisionOut
    component: DesignComponent


class DesignNetPin(BaseModel):
    reference: str
    pin: str
    pinName: str = ""


class DesignNet(BaseModel):
    name: str
    netUid: Optional[str] = None
    netClass: str = ""
    aliases: List[str] = Field(default_factory=list)
    components: List[str] = Field(default_factory=list)
    pins: List[DesignNetPin] = Field(default_factory=list)


class DesignNetOut(BaseModel):
    revision: DesignRevisionOut
    net: DesignNet


@router.get("/{project_id}/design/search", response_model=DesignSearchOut)
async def search_design(
    project_id: str,
    response: Response,
    q: str = Query(..., min_length=1),
    kind: Optional[List[Literal["component", "net"]]] = Query(default=None),
    revision: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    user: AuthenticatedUser = Depends(require_viewer),
):
    """Find components and nets matching ``q`` at ``revision``."""

    project = get_project_for_role_or_404(project_id, user.role)
    commit = await _resolve(project, revision)
    index, design_key = await asyncio.to_thread(design_index.load, project, commit)
    results = await asyncio.to_thread(design_index.find, index, q, set(kind or _DEFAULT_KINDS), limit=limit)
    response.headers["Cache-Control"] = _cache_control(revision)
    return {
        "revision": _revision(commit, design_key),
        "query": q,
        "results": results,
    }


@router.get("/{project_id}/design/components/{ref}", response_model=DesignComponentOut)
async def get_design_component(
    project_id: str,
    response: Response,
    ref: str,
    revision: Optional[str] = Query(default=None),
    user: AuthenticatedUser = Depends(require_viewer),
):
    """One component's fields, footprint, sheet, assembly state and pins."""

    project = get_project_for_role_or_404(project_id, user.role)
    commit = await _resolve(project, revision)
    index, design_key = await asyncio.to_thread(design_index.load, project, commit)
    found = await asyncio.to_thread(design_index.component, index, ref)
    if found is None:
        raise NotFound("Component not found", code="component-not-found")
    response.headers["Cache-Control"] = _cache_control(revision)
    return {"revision": _revision(commit, design_key), "component": found}


@router.get("/{project_id}/design/nets/{name:path}", response_model=DesignNetOut)
async def get_design_net(
    project_id: str,
    response: Response,
    name: str,
    revision: Optional[str] = Query(default=None),
    user: AuthenticatedUser = Depends(require_viewer),
):
    """One net's pins and connected components (net names may contain ``/``)."""

    project = get_project_for_role_or_404(project_id, user.role)
    commit = await _resolve(project, revision)
    index, design_key = await asyncio.to_thread(design_index.load, project, commit)
    found = await asyncio.to_thread(design_index.net, index, name)
    if found is None:
        raise NotFound("Net not found", code="net-not-found")
    response.headers["Cache-Control"] = _cache_control(revision)
    return {"revision": _revision(commit, design_key), "net": found}


async def _resolve(project: Any, revision: Optional[str]) -> str:
    """Resolve ``revision`` off the event loop; an unknown name is a 400."""

    try:
        return await asyncio.to_thread(design_revision.resolve, project, revision)
    except ValueError as error:
        raise BadRequest("Unknown revision", code="revision-unknown") from error


def _revision(commit: str, design_key: Optional[str]) -> dict[str, Any]:
    return {"commit": commit, "designKey": design_key}


def _cache_control(revision: Optional[str]) -> str:
    """A full sha is immutable; every other revision may move."""

    return PINNED_CACHE_CONTROL if design_revision.is_full_sha(revision) else "private, no-cache"
