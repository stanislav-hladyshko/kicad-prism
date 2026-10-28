"""The engineering BOM of /api/projects (ADR scripting-and-mcp §3.8).

The route reads the revision's semantic index and serves the same grouped BOM
the UI computes (`frontend/src/lib/engineering-bom.ts`). Both sides are held to
`fixtures/bom/`, so an API client and a person see one answer.

The project lookup is role-aware. The index is read and parsed in a worker
thread: it is the multi-megabyte bundle member, and the event loop must not
inflate it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api._helpers import get_project_for_role_or_404
from app.core.errors import caller_errors
from app.core.security import AuthenticatedUser, require_viewer
from app.services import semantic_index_service
from app.services.design_query import revision as design_revision
from app.services.design_query.bom import build_bom

router = APIRouter(dependencies=[Depends(require_viewer)])


class BomLineOut(BaseModel):
    key: str
    quantity: int
    references: list[str]
    fields: dict[str, str]


class BomOut(BaseModel):
    #: The commit read: the one requested, or the checkout's HEAD resolved to
    #: its full id, so a caller can pin later reads to it.
    commit: Optional[str]
    #: The bundle's design revision key.
    designKey: str
    columns: list[str]
    lines: list[BomLineOut]


def _read_index(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


@router.get("/{project_id}/bom", response_model=BomOut)
async def get_project_bom(
    project_id: str,
    commit: Optional[str] = Query(default=None),
    assembly: Literal["all", "assembly"] = Query(default="all"),
    user: AuthenticatedUser = Depends(require_viewer),
) -> BomOut:
    project = get_project_for_role_or_404(project_id, user.role)
    with caller_errors((ValueError, 400), (RuntimeError, 503)):
        commit = await asyncio.to_thread(design_revision.resolve, project, commit)
        artifact = await asyncio.to_thread(
            semantic_index_service.artifact_file,
            project,
            commit,
        )
        path, design_key = artifact
        index = await asyncio.to_thread(_read_index, path)
        bom = build_bom(index.get("components") or [], assembly)
    return BomOut(commit=commit, designKey=str(design_key), **bom)
