"""Comments anchored by design identity (ADR scripting-and-mcp §10 D16).

``POST .../comments/anchored`` is the write an agent uses when it reviewed a
component, a net or a schematic sheet and has no pointer coordinates: it names
the design element and the browser's own comment row is created from the
revision's bundle geometry. The equivalent browser route is
``POST .../comments``; both end in ``comments_store.create_comment``.

The route lives beside ``app/api/comments.py`` because that module is at the
module-size limit; it reuses its request normalisers and ``require_comment_writer``
so the two writes refuse the same inputs and need the same scope.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api._helpers import get_project_for_role_or_404
from app.api.comments import (
    _actor,
    _normalize_comment_class,
    _normalize_content,
    _normalize_severity,
)
from app.api.comments_models import CommentView
from app.core.errors import BadRequest
from app.core.security import AuthenticatedUser, require_comment_writer, require_viewer
from app.services.comment_anchor_service import resolve_canvas_anchor
from app.services.comment_design_anchor_service import resolve_anchor
from app.services.comments_store_service import (
    DEFAULT_COMMENT_CLASS,
    DEFAULT_COMMENT_SEVERITY,
    comments_store,
)
from app.services.design_query import revision as design_revision

router = APIRouter(dependencies=[Depends(require_viewer)])


class AnchoredCommentRequest(BaseModel):
    """A comment for one design element, named instead of clicked.

    Exactly one of ``component``, ``net`` or ``sheet`` is required; the route
    answers 400 ``invalid-anchor`` otherwise, so the refusal carries the typed
    error the MCP tool surfaces rather than a validation array.
    """

    content: str
    commit: Optional[str] = None
    component: Optional[str] = None
    net: Optional[str] = None
    sheet: Optional[str] = None
    view: Optional[str] = None
    commentClass: Optional[str] = DEFAULT_COMMENT_CLASS
    severity: Optional[str] = DEFAULT_COMMENT_SEVERITY


@router.post(
    "/{project_id}/comments/anchored",
    dependencies=[Depends(require_comment_writer)],
    response_model=CommentView,
    response_model_exclude_unset=True,
    summary="Create a comment anchored by design identity",
)
async def create_anchored_comment(
    project_id: str,
    request: AnchoredCommentRequest,
    user: AuthenticatedUser = Depends(require_viewer),
):
    """Resolve a component, net or sheet in the revision's bundle, then comment on it."""
    content = _normalize_content(request.content)
    comment_class = _normalize_comment_class(request.commentClass)
    severity = _normalize_severity(request.severity)

    def write():
        project = get_project_for_role_or_404(project_id, user.role)
        try:
            commit = design_revision.resolve(project, request.commit)
        except ValueError:
            raise BadRequest("Unknown revision", code="revision-unknown") from None
        anchor = resolve_anchor(
            project,
            commit=commit,
            component=request.component or "",
            net=request.net or "",
            sheet=request.sheet or "",
            view=request.view or "",
            requested_by=user.email,
        )
        canvas = resolve_canvas_anchor(project, revision={"commit": commit}, context=anchor.context)
        return comments_store.create_comment(
            project_id=project.id,
            project_path=project.path,
            context=anchor.context,
            location=anchor.location(),
            content=content,
            actor=_actor(user),
            element_id=anchor.element_id,
            element_ref=anchor.element_ref,
            element_type=anchor.element_type,
            comment_class=comment_class,
            severity=severity,
            file_path=canvas.file_path,
            anchor_commit=canvas.commit,
            anchor_revision_key=canvas.source_revision_key,
            anchor_design_key=canvas.design_key,
            anchor_source=canvas.source,
            project_relative_path=canvas.project_relative_path,
        )

    return await asyncio.to_thread(write)


__all__ = ["router"]
