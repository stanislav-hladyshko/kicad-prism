"""
Comments API for KiCAD-Prism Collaboration Feature.

Comment CRUD is backed by the PostgreSQL ``comments`` schema. Visualizer markers
are published via ecad-viewer overlay scenes (never written into KiCad sources).
"""

import asyncio
import math
import os
import re
from typing import Callable, List, Optional, TypeVar

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, model_validator

from app.api._helpers import get_project_for_role_or_404
from app.api.comments_models import (
    CommentAttachmentUploadView,
    CommentHistoryEntry,
    CommentThreadView,
    CommentView,
    CommentsListing,
    DeletedCommentView,
    PushCommentsView,
    ReplyCreatedView,
)
from app.core import preconditions
from app.core.security import AuthenticatedUser, require_comment_writer, require_designer, require_viewer
from app.services import access_service, comment_attachments, comment_permissions, comment_social
from app.services.comment_capabilities import with_capabilities
from app.services.comment_anchor_service import (
    AnchorValidationError,
    resolve_canvas_anchor,
    resolve_comparison_anchor,
    resolve_manual_pin,
    resolve_displayed_bindings,
)
from app.services.comment_permissions import ActorIdentity, CommentAction, CommentPermissionError
from app.services.comments_store_service import (
    COMMENT_CLASSES,
    COMMENT_SEVERITIES,
    DEFAULT_COMMENT_CLASS,
    DEFAULT_COMMENT_SEVERITY,
    comments_store,
)

router = APIRouter(dependencies=[Depends(require_viewer)])
T = TypeVar("T")


# ============================================================
# PYDANTIC MODELS
# ============================================================

class CommentLocation(BaseModel):
    x: float
    y: float
    layer: str = ""
    page: str = ""
    bounds: Optional[List[float]] = None  # [x, y, w, h] for area comments


class CreateDisplayedRevision(BaseModel):
    commit: Optional[str] = None
    worktree: bool = False
    sourceRevisionKey: Optional[str] = None


class CreateCommentRequest(BaseModel):
    context: str  # "PCB" or "SCH"
    location: CommentLocation
    content: str
    contentFormat: Optional[str] = None
    elementId: Optional[str] = None
    elementRef: Optional[str] = None
    elementType: Optional[str] = None
    commentClass: Optional[str] = DEFAULT_COMMENT_CLASS
    severity: Optional[str] = DEFAULT_COMMENT_SEVERITY
    mentions: Optional[List[str]] = None
    metadata: Optional[dict] = None
    revision: Optional[CreateDisplayedRevision] = None


class CreateReplyRequest(BaseModel):
    content: str
    contentFormat: Optional[str] = None


class UpdateReplyRequest(BaseModel):
    content: str
    contentFormat: Optional[str] = None


class UpdateCommentRequest(BaseModel):
    status: Optional[str] = None  # "OPEN" or "RESOLVED"
    content: Optional[str] = None
    contentFormat: Optional[str] = None
    severity: Optional[str] = None
    commentClass: Optional[str] = None
    mentions: Optional[List[str]] = None

    @model_validator(mode="before")
    @classmethod
    def reject_immutable_anchor_fields(cls, data):
        if isinstance(data, dict) and any(key in data for key in ("location", "revision", "anchor")):
            raise ValueError("anchor fields are immutable")
        return data


class PinCommentRequest(BaseModel):
    commit: str


class ReattachCommentRequest(BaseModel):
    commit: str
    location: CommentLocation
    elementId: Optional[str] = None
    relativePoint: Optional[List[float]] = None


class CommentAnchor(BaseModel):
    state: str = "unpinned"
    source: Optional[str] = None
    commit: Optional[str] = None
    sourceRevisionKey: Optional[str] = None
    baseCommit: Optional[str] = None
    compareCommit: Optional[str] = None
    selectedSide: Optional[str] = None
    projectFile: Optional[str] = None


class CommentPermissions(BaseModel):
    canReply: bool = True
    canEdit: bool = False
    canDelete: bool = False
    canResolve: bool = False
    canPublish: bool = False


class CommentReply(BaseModel):
    id: Optional[str] = None
    author: str
    authorUserId: Optional[str] = None
    authorKind: str = "legacy"
    timestamp: str
    updatedAt: Optional[str] = None
    revision: int = 1
    content: str
    origin: str = "prism"
    deletedAt: Optional[str] = None
    permissions: Optional[CommentPermissions] = None


class Comment(BaseModel):
    id: str
    author: str
    authorUserId: Optional[str] = None
    authorKind: str = "legacy"
    timestamp: str
    updatedAt: Optional[str] = None
    revision: int = 1
    status: str
    context: str
    location: CommentLocation
    content: str
    replies: List[CommentReply] = Field(default_factory=list)
    elementId: Optional[str] = None
    elementRef: Optional[str] = None
    elementType: Optional[str] = None
    commentClass: str = DEFAULT_COMMENT_CLASS
    severity: str = DEFAULT_COMMENT_SEVERITY
    mentions: List[str] = Field(default_factory=list)
    scope: str = "canvas"
    baseCommit: Optional[str] = None
    compareCommit: Optional[str] = None
    comparisonDomain: Optional[str] = None
    filePath: Optional[str] = None
    semanticItemId: Optional[str] = None
    anchorKind: Optional[str] = None
    # Reserved for future forge Issues sync (null/omitted until enabled).
    forgeProvider: Optional[str] = None
    forgeIssueId: Optional[str] = None
    forgeIssueUrl: Optional[str] = None
    forgeSyncState: Optional[str] = None
    anchor: CommentAnchor = Field(default_factory=CommentAnchor)
    permissions: Optional[CommentPermissions] = None


class CommentsMeta(BaseModel):
    version: str = "1.1"
    generator: str = "KiCad-Prism-Web"


class CommentsFile(BaseModel):
    meta: CommentsMeta = Field(default_factory=CommentsMeta)
    comments: List[Comment] = Field(default_factory=list)


class MentionCandidate(BaseModel):
    email: str
    role: str


class CreateComparisonCommentRequest(BaseModel):
    baseCommit: str
    compareCommit: str
    domain: str
    content: str
    contentFormat: Optional[str] = None
    filePath: Optional[str] = None
    semanticItemId: Optional[str] = None
    semanticItemRef: Optional[str] = None
    anchorKind: str = "comparison"
    commentClass: Optional[str] = DEFAULT_COMMENT_CLASS
    severity: Optional[str] = DEFAULT_COMMENT_SEVERITY
    mentions: Optional[List[str]] = None
    selectedSide: Optional[str] = None


def _actor(user: AuthenticatedUser) -> ActorIdentity:
    return comment_permissions.resolve_actor(user)


def _read_actor(user: AuthenticatedUser) -> Optional[ActorIdentity]:
    try:
        return _actor(user)
    except CommentPermissionError:
        return None


async def _run_mutation(write: Callable[[], T]) -> T:
    """Run a blocking write off the event loop. Its refusals (permission, publication,
    revision conflict, anchor, attachment) are typed errors the app handler renders."""

    return await asyncio.to_thread(write)


def _normalize_context(context: str) -> str:
    normalized = context.upper().strip()
    if normalized not in {"PCB", "SCH"}:
        raise HTTPException(status_code=400, detail="Context must be 'PCB' or 'SCH'")
    return normalized


def _normalize_content(content: str, *, field: str = "content") -> str:
    normalized = content.strip()
    if not normalized:
        raise HTTPException(status_code=400, detail=f"{field.capitalize()} cannot be empty")
    if len(normalized) > comment_attachments.MAX_CONTENT_CHARS:
        raise HTTPException(status_code=413, detail=f"{field.capitalize()} is too long")
    return normalized


def _content_format(value: Optional[str], *, default: Optional[str] = comment_attachments.CONTENT_FORMAT_PLAIN) -> Optional[str]:
    if value is None:
        return default
    try:
        return comment_attachments.normalize_content_format(value)
    except comment_attachments.AttachmentError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc


def _normalize_bounds(bounds: Optional[List[float]]) -> Optional[List[float]]:
    if bounds is None:
        return None
    if len(bounds) != 4:
        raise HTTPException(status_code=400, detail="location.bounds must be [x, y, w, h]")
    try:
        x, y, w, h = (float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3]))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="location.bounds must be numeric")
    if w <= 0 or h <= 0:
        raise HTTPException(status_code=400, detail="location.bounds width/height must be > 0")
    return [x, y, w, h]


def _normalize_comment_class(value: Optional[str]) -> str:
    normalized = (value or DEFAULT_COMMENT_CLASS).strip().lower()
    if normalized not in COMMENT_CLASSES:
        raise HTTPException(
            status_code=400,
            detail=f"commentClass must be one of: {', '.join(COMMENT_CLASSES)}",
        )
    return normalized


def _normalize_severity(value: Optional[str]) -> str:
    normalized = (value or DEFAULT_COMMENT_SEVERITY).strip().lower()
    if normalized not in COMMENT_SEVERITIES:
        raise HTTPException(
            status_code=400,
            detail=f"severity must be one of: {', '.join(COMMENT_SEVERITIES)}",
        )
    return normalized


def _normalize_commit(value: str, field: str) -> str:
    normalized = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", normalized):
        raise HTTPException(status_code=400, detail=f"{field} must be a full commit SHA")
    return normalized


def _normalize_anchor_kind(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"comparison", "file", "item", "group"}:
        raise HTTPException(
            status_code=400,
            detail="anchorKind must be comparison, file, item, or group",
        )
    return normalized


# ============================================================
# API ENDPOINTS
# ============================================================

@router.get("/{project_id}/comments/mention-candidates", response_model=List[MentionCandidate])
async def list_mention_candidates(
    project_id: str,
    user: AuthenticatedUser = Depends(require_viewer),
):
    """
    Workspace-scoped users available for @mentions in comments.
    Requires project access; list itself is instance-wide role assignments.
    """
    def read() -> List[MentionCandidate]:
        get_project_for_role_or_404(project_id, user.role)
        return [
            MentionCandidate(email=item["email"], role=item["role"])
            for item in access_service.list_role_assignments()
        ]

    return await asyncio.to_thread(read)


@router.get(
    "/{project_id}/comments",
    response_model=CommentsListing,
    response_model_exclude_unset=True,
    summary="List a project's comment threads",
)
async def get_comments(
    project_id: str,
    user: AuthenticatedUser = Depends(require_viewer),
    revision: Optional[str] = None,
):
    """
    Get all comments for a project from DB snapshot.
    """
    def read():
        project = get_project_for_role_or_404(project_id, user.role)
        listing = comments_store.get_comments_file(project.id, project.path)
        if revision:
            displayed = _normalize_commit(revision, "revision")
            bindings = comments_store.get_anchor_bindings(
                project.id, [comment["id"] for comment in listing["comments"]],
            )
            listing["comments"] = resolve_displayed_bindings(
                project, listing["comments"], displayed, bindings,
                record_design_keys=lambda keys: comments_store.fill_anchor_design_keys(project.id, keys),
            )
        actor = _read_actor(user)
        listing["comments"] = [
            with_capabilities(comment, actor) for comment in listing["comments"]
        ]
        return listing

    try:
        return await asyncio.to_thread(read)
    except AnchorValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc


@router.get(
    "/{project_id}/comments/{comment_id}/thread",
    response_model=CommentThreadView,
    response_model_exclude_unset=True,
    summary="Get one comment thread for a live refresh",
)
async def get_comment_thread(
    project_id: str,
    comment_id: str,
    user: AuthenticatedUser = Depends(require_viewer),
    revision: Optional[str] = None,
):
    """One thread, shaped like a listing entry, for applying a live change.

    Returns ``{"comment": null}`` when the thread no longer exists. ``revision``
    resolves a canvas anchor exactly as the listing does.
    """
    def read():
        project = get_project_for_role_or_404(project_id, user.role)
        comment, cursor = comments_store.get_thread(project.id, project.path, comment_id)
        if comment is not None and revision and comment.get("scope") != "comparison":
            displayed = _normalize_commit(revision, "revision")
            bindings = comments_store.get_anchor_bindings(project.id, [comment_id])
            [comment] = resolve_displayed_bindings(
                project, [comment], displayed, bindings,
                record_design_keys=lambda keys: comments_store.fill_anchor_design_keys(project.id, keys),
            )
        actor = _read_actor(user)
        if comment is not None:
            comment = with_capabilities(comment, actor)
        return {"comment": comment, "cursor": cursor}

    try:
        return await asyncio.to_thread(read)
    except AnchorValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc


@router.get(
    "/{project_id}/comparison-comments",
    response_model=CommentsListing,
    response_model_exclude_unset=True,
    summary="List a comparison's discussion threads",
)
async def get_comparison_comments(
    project_id: str,
    base: str,
    compare: str,
    domain: Optional[str] = None,
    user: AuthenticatedUser = Depends(require_viewer),
):
    """List discussion threads for one immutable, explicitly ordered comparison."""
    domain_norm = _normalize_context(domain) if domain else None
    base_commit = _normalize_commit(base, "base")
    compare_commit = _normalize_commit(compare, "compare")

    def read():
        project = get_project_for_role_or_404(project_id, user.role)
        listing = comments_store.get_comparison_comments(
            project_id=project.id,
            project_path=project.path,
            base_commit=base_commit,
            compare_commit=compare_commit,
            comparison_domain=domain_norm,
        )
        actor = _read_actor(user)
        listing["comments"] = [
            with_capabilities(comment, actor) for comment in listing["comments"]
        ]
        return listing

    return await asyncio.to_thread(read)


@router.get(
    "/{project_id}/comparison-comments/report",
    response_class=Response,
    responses={
        200: {
            "content": {"application/zip": {}},
            "description": "The discussion as Markdown plus its attachments, zipped",
        }
    },
    summary="Download a comparison's discussion report",
)
async def get_comparison_discussion_report(
    project_id: str,
    base: str,
    compare: str,
    domain: Optional[str] = None,
    user: AuthenticatedUser = Depends(require_viewer),
):
    """The comparison's discussion as Markdown with its attachments, zipped."""
    domain_norm = _normalize_context(domain) if domain else None
    base_commit = _normalize_commit(base, "base")
    compare_commit = _normalize_commit(compare, "compare")

    def build() -> tuple[bytes, str]:
        project = get_project_for_role_or_404(project_id, user.role)
        name = project.display_name or project.name
        scope = f" ({domain_norm})" if domain_norm else ""
        title = f"{name}: review discussion {base_commit[:10]} → {compare_commit[:10]}{scope}"
        return comments_store.comparison_discussion_report(
            project.id, project.path, base_commit, compare_commit, domain_norm, title,
        ), name

    data, name = await asyncio.to_thread(build)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "project"
    filename = f"{safe}-discussion-{base_commit[:10]}-{compare_commit[:10]}.zip"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "X-Content-Type-Options": "nosniff"},
    )


@router.post(
    "/{project_id}/comparison-comments",
    dependencies=[Depends(require_comment_writer)],
    response_model=CommentView,
    response_model_exclude_unset=True,
    summary="Create a comparison, file, group or item discussion",
)
async def create_comparison_comment(
    project_id: str,
    request: CreateComparisonCommentRequest,
    user: AuthenticatedUser = Depends(require_viewer),
):
    """Create a comparison-, file-, group-, or semantic-item discussion."""
    domain = _normalize_context(request.domain)
    anchor_kind = _normalize_anchor_kind(request.anchorKind)
    if anchor_kind in {"item", "group"} and not request.semanticItemId:
        raise HTTPException(
            status_code=400,
            detail="semanticItemId is required for item and group comments",
        )
    content = _normalize_content(request.content)
    content_format = _content_format(request.contentFormat)
    comment_class = _normalize_comment_class(request.commentClass)
    severity = _normalize_severity(request.severity)

    def write():
        project = get_project_for_role_or_404(project_id, user.role)
        actor = _actor(user)
        anchor = resolve_comparison_anchor(
            project, base_commit=request.baseCommit, compare_commit=request.compareCommit,
            selected_side=request.selectedSide, file_path=request.filePath, context=domain,
        )
        return comments_store.create_comment(
            project_id=project.id,
            project_path=project.path,
            context=domain,
            location={"x": 0.0, "y": 0.0, "layer": "", "page": request.filePath or ""},
            content=content,
            content_format=content_format,
            actor=actor,
            element_id=request.semanticItemId,
            element_ref=request.semanticItemRef,
            element_type=anchor_kind,
            comment_class=comment_class,
            severity=severity,
            mentions=request.mentions,
            scope="comparison",
            base_commit=anchor.base_commit,
            compare_commit=anchor.compare_commit,
            comparison_domain=domain,
            file_path=anchor.file_path,
            semantic_item_id=request.semanticItemId,
            anchor_kind=anchor_kind,
            anchor_commit=anchor.commit,
            anchor_revision_key=anchor.source_revision_key,
            anchor_design_key=anchor.design_key,
            anchor_source=anchor.source,
            selected_side=anchor.selected_side,
            project_relative_path=anchor.project_relative_path,
        )

    return await _run_mutation(write)


@router.post(
    "/{project_id}/comments",
    dependencies=[Depends(require_comment_writer)],
    response_model=CommentView,
    response_model_exclude_unset=True,
    summary="Create a comment on the design",
)
async def create_comment(
    project_id: str,
    request: CreateCommentRequest,
    user: AuthenticatedUser = Depends(require_viewer),
):
    """
    Create a new comment on the design.
    """
    context = _normalize_context(request.context)
    content = _normalize_content(request.content)
    content_format = _content_format(request.contentFormat)
    location = request.location.model_dump()
    location["bounds"] = _normalize_bounds(request.location.bounds)
    comment_class = _normalize_comment_class(request.commentClass)
    severity = _normalize_severity(request.severity)
    metadata = dict(request.metadata or {})
    relative_point = metadata.get("anchorRelativePoint")
    if relative_point is not None and (
        not request.elementId
        or not isinstance(relative_point, list)
        or len(relative_point) != 2
        or any(
            not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or value < 0 or value > 1
            for value in relative_point
        )
    ):
        raise HTTPException(status_code=400, detail="anchorRelativePoint requires an object and two values between 0 and 1")

    def write():
        project = get_project_for_role_or_404(project_id, user.role)
        actor = _actor(user)
        anchor = resolve_canvas_anchor(
            project, revision=request.revision.model_dump() if request.revision else None,
            context=context,
        )
        return comments_store.create_comment(
            project_id=project.id,
            project_path=project.path,
            context=context,
            location=location,
            content=content,
            content_format=content_format,
            actor=actor,
            element_id=request.elementId,
            element_ref=request.elementRef,
            element_type=request.elementType,
            comment_class=comment_class,
            severity=severity,
            mentions=request.mentions,
            metadata=metadata,
            file_path=anchor.file_path,
            anchor_commit=anchor.commit,
            anchor_revision_key=anchor.source_revision_key,
            anchor_design_key=anchor.design_key,
            anchor_source=anchor.source,
            project_relative_path=anchor.project_relative_path,
        )

    return await _run_mutation(write)


def _expected_revision(http: Request, kind: str, target_id: str, *, required: bool = False) -> Optional[int]:
    """The revision ``If-Match`` names (``"comment:<id>:<n>"`` / ``"reply:<id>:<n>"``); ``None`` when optional and absent."""
    return preconditions.int_version(preconditions.if_match(http, kind, target_id, required=required))


@router.patch(
    "/{project_id}/comments/{comment_id}",
    dependencies=[Depends(require_comment_writer)],
    response_model=CommentView,
    response_model_exclude_unset=True,
    summary="Edit a comment's content or status",
)
async def update_comment(
    project_id: str,
    comment_id: str,
    request: UpdateCommentRequest,
    http: Request,
    user: AuthenticatedUser = Depends(require_viewer),
):
    """Edit content or status using optimistic comment revisions."""
    has_edit = any(value is not None for value in (
        request.content, request.severity, request.commentClass, request.mentions,
    ))
    if request.status is None and not has_edit:
        raise HTTPException(status_code=400, detail="No update fields provided")

    status = request.status.upper() if request.status is not None else None
    if status is not None and status not in {"OPEN", "RESOLVED"}:
        raise HTTPException(status_code=400, detail="Status must be 'OPEN' or 'RESOLVED'")
    content = _normalize_content(request.content) if request.content is not None else None
    content_format = _content_format(request.contentFormat, default=None)
    severity = _normalize_severity(request.severity) if request.severity is not None else None
    comment_class = _normalize_comment_class(request.commentClass) if request.commentClass is not None else None
    expected_revision = _expected_revision(http, "comment", comment_id)

    def write():
        project = get_project_for_role_or_404(project_id, user.role)
        return comments_store.patch_comment(
            project.id, project.path, comment_id, actor=_actor(user),
            expected_revision=expected_revision, content=content, severity=severity,
            comment_class=comment_class, mentions=request.mentions, status=status,
            content_format=content_format,
        )

    updated_comment = await _run_mutation(write)

    if not updated_comment:
        raise HTTPException(status_code=404, detail="Comment not found")

    return updated_comment


@router.post(
    "/{project_id}/comments/{comment_id}/pin",
    dependencies=[Depends(require_comment_writer)],
    response_model=CommentView,
    response_model_exclude_unset=True,
    summary="Pin an unpinned comment to an exact commit",
)
async def pin_comment(
    project_id: str,
    comment_id: str,
    request: PinCommentRequest,
    http: Request,
    user: AuthenticatedUser = Depends(require_viewer),
):
    """An admin may pin a legacy or worktree comment once, to an exact commit."""
    expected_revision = _expected_revision(http, "comment", comment_id)

    def write():
        project = get_project_for_role_or_404(project_id, user.role)
        return comments_store.pin_comment_anchor(
            project.id, project.path, comment_id, actor=_actor(user), commit=request.commit,
            resolve=lambda context: resolve_manual_pin(project, commit=request.commit, context=context),
            expected_revision=expected_revision,
        )

    result = await _run_mutation(write)
    if result is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    return result


@router.post(
    "/{project_id}/comments/{comment_id}/reattach",
    dependencies=[Depends(require_comment_writer)],
    response_model=CommentView,
    response_model_exclude_unset=True,
    summary="Reattach a comment for a commit's descendants",
)
async def reattach_comment(
    project_id: str,
    comment_id: str,
    request: ReattachCommentRequest,
    http: Request,
    user: AuthenticatedUser = Depends(require_viewer),
):
    """Append a binding for this commit's descendants; preserve the origin."""
    expected_revision = _expected_revision(http, "comment", comment_id, required=True)
    location = request.location.model_dump()
    if not all(math.isfinite(float(location[key])) for key in ("x", "y")):
        raise HTTPException(status_code=400, detail="Comment location must be finite")
    location["bounds"] = _normalize_bounds(location.get("bounds"))
    relative_point = request.relativePoint
    if relative_point is not None and (
        len(relative_point) != 2
        or any(not math.isfinite(value) or value < 0 or value > 1 for value in relative_point)
        or not request.elementId
    ):
        raise HTTPException(status_code=400, detail="relativePoint requires an object and two values between 0 and 1")

    def write():
        project = get_project_for_role_or_404(project_id, user.role)
        return comments_store.reattach_comment(
            project.id, project.path, comment_id, actor=_actor(user),
            commit=request.commit, location=location,
            element_id=request.elementId, relative_point=relative_point,
            resolve=lambda context: resolve_canvas_anchor(
                project, revision={"commit": request.commit}, context=context,
            ),
            expected_revision=expected_revision,
        )

    result = await _run_mutation(write)
    if result is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    return result


@router.post(
    "/{project_id}/comments/{comment_id}/replies",
    dependencies=[Depends(require_comment_writer)],
    response_model=ReplyCreatedView,
    response_model_exclude_unset=True,
    summary="Reply to an existing comment",
)
async def add_reply(
    project_id: str,
    comment_id: str,
    request: CreateReplyRequest,
    user: AuthenticatedUser = Depends(require_viewer),
):
    """
    Add a reply to an existing comment.
    """
    content = _normalize_content(request.content)
    content_format = _content_format(request.contentFormat)
    def write():
        project = get_project_for_role_or_404(project_id, user.role)
        result = comments_store.add_reply(
            project_id=project.id,
            project_path=project.path,
            comment_id=comment_id,
            content=content,
            content_format=content_format,
            actor=_actor(user),
        )
        if result is None:
            return None
        comment, reply = result
        return {"comment": comment, "reply": reply}

    result = await _run_mutation(write)

    if not result:
        raise HTTPException(status_code=404, detail="Comment not found")

    return result


@router.patch(
    "/{project_id}/comments/{comment_id}/replies/{reply_id}",
    dependencies=[Depends(require_comment_writer)],
    response_model=CommentView,
    response_model_exclude_unset=True,
    summary="Edit a reply",
)
async def update_reply(
    project_id: str,
    comment_id: str,
    reply_id: str,
    request: UpdateReplyRequest,
    http: Request,
    user: AuthenticatedUser = Depends(require_viewer),
):
    content = _normalize_content(request.content)
    content_format = _content_format(request.contentFormat, default=None)
    expected_revision = _expected_revision(http, "reply", reply_id)

    def write():
        project = get_project_for_role_or_404(project_id, user.role)
        return comments_store.edit_reply(
            project.id, project.path, comment_id, reply_id, content, actor=_actor(user),
            expected_revision=expected_revision, content_format=content_format,
        )

    result = await _run_mutation(write)
    if result is None:
        raise HTTPException(status_code=404, detail="Reply not found")
    return result


@router.delete(
    "/{project_id}/comments/{comment_id}/replies/{reply_id}",
    dependencies=[Depends(require_comment_writer)],
    response_model=CommentView,
    response_model_exclude_unset=True,
    summary="Delete a reply",
)
async def delete_reply(
    project_id: str,
    comment_id: str,
    reply_id: str,
    http: Request,
    user: AuthenticatedUser = Depends(require_viewer),
):
    expected_revision = _expected_revision(http, "reply", reply_id)

    def write():
        project = get_project_for_role_or_404(project_id, user.role)
        return comments_store.delete_reply(
            project.id, project.path, comment_id, reply_id, actor=_actor(user),
            expected_revision=expected_revision,
        )

    result = await _run_mutation(write)
    if result is None:
        raise HTTPException(status_code=404, detail="Reply not found")
    return result


async def _set_reaction(
    project_id: str, comment_id: str, reply_id: Optional[str], reaction: str,
    user: AuthenticatedUser, *, present: bool,
):
    if reaction not in comment_social.REACTIONS:
        raise HTTPException(status_code=422, detail="Unknown reaction")

    def write():
        project = get_project_for_role_or_404(project_id, user.role)
        return comments_store.set_reaction(
            project.id, project.path, comment_id, reply_id,
            actor=_actor(user), reaction=reaction, present=present,
        )

    result = await _run_mutation(write)
    if result is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    return result


@router.put(
    "/{project_id}/comments/{comment_id}/reactions/{reaction}",
    dependencies=[Depends(require_comment_writer)],
    response_model=CommentView,
    response_model_exclude_unset=True,
    summary="Add a reaction to a comment or reply",
)
async def add_comment_reaction(
    project_id: str, comment_id: str, reaction: str, replyId: Optional[str] = None,
    user: AuthenticatedUser = Depends(require_viewer),
):
    """React to a root, or to one of its replies with ``replyId``. Idempotent."""
    return await _set_reaction(project_id, comment_id, replyId, reaction, user, present=True)


@router.delete(
    "/{project_id}/comments/{comment_id}/reactions/{reaction}",
    dependencies=[Depends(require_comment_writer)],
    response_model=CommentView,
    response_model_exclude_unset=True,
    summary="Remove a reaction from a comment or reply",
)
async def remove_comment_reaction(
    project_id: str, comment_id: str, reaction: str, replyId: Optional[str] = None,
    user: AuthenticatedUser = Depends(require_viewer),
):
    return await _set_reaction(project_id, comment_id, replyId, reaction, user, present=False)


@router.delete(
    "/{project_id}/comments/{comment_id}",
    dependencies=[Depends(require_comment_writer)],
    response_model=DeletedCommentView,
    response_model_exclude_unset=True,
    summary="Delete a comment",
)
async def delete_comment(
    project_id: str,
    comment_id: str,
    http: Request,
    user: AuthenticatedUser = Depends(require_viewer),
):
    """
    Delete a comment.
    """
    expected_revision = _expected_revision(http, "comment", comment_id)

    def write():
        project = get_project_for_role_or_404(project_id, user.role)
        return comments_store.delete_comment(
            project_id=project.id,
            project_path=project.path,
            comment_id=comment_id,
            expected_revision=expected_revision,
            actor=_actor(user),
        )

    deleted = await _run_mutation(write)

    if not deleted:
        raise HTTPException(status_code=404, detail="Comment not found")

    return {"deleted": comment_id}


@router.get(
    "/{project_id}/comments/{comment_id}/history",
    response_model=list[CommentHistoryEntry],
    response_model_exclude_unset=True,
    summary="List a comment's revisions",
)
async def get_comment_history(
    project_id: str,
    comment_id: str,
    user: AuthenticatedUser = Depends(require_viewer),
):
    def read():
        project = get_project_for_role_or_404(project_id, user.role)
        return comments_store.get_history(project.id, "root", comment_id)

    return await asyncio.to_thread(read)


@router.get(
    "/{project_id}/comments/{comment_id}/replies/{reply_id}/history",
    response_model=list[CommentHistoryEntry],
    response_model_exclude_unset=True,
    summary="List a reply's revisions",
)
async def get_reply_history(
    project_id: str,
    comment_id: str,
    reply_id: str,
    user: AuthenticatedUser = Depends(require_viewer),
):
    def read():
        project = get_project_for_role_or_404(project_id, user.role)
        return comments_store.get_history(project.id, "reply", reply_id)

    return await asyncio.to_thread(read)


# ============================================================
# EXPORT ENDPOINT
# ============================================================

@router.post(
    "/{project_id}/comments/push",
    dependencies=[Depends(require_designer)],
    response_model=PushCommentsView,
    response_model_exclude_unset=True,
    summary="Export the comment snapshot to the project's comments.json",
)
async def push_comments(project_id: str, user: AuthenticatedUser = Depends(require_viewer)):
    """
    Export DB snapshot to comments.json artifact only.
    Git commit/push is intentionally left to the user workflow.
    """
    def export() -> str:
        project = get_project_for_role_or_404(project_id, user.role)
        comments_path = comments_store.export_comments_json(project.id, project.path)
        return os.path.relpath(comments_path, project.path)

    try:
        comments_rel_path = await asyncio.to_thread(export)

        return {
            "success": True,
            "message": "Generated comments artifact from DB snapshot.",
            "comments_path": comments_rel_path,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to export comments") from exc


# ============================================================
# ATTACHMENTS
# ============================================================

@router.post(
    "/{project_id}/comment-attachments",
    dependencies=[Depends(require_comment_writer)],
    response_model=CommentAttachmentUploadView,
    response_model_exclude_unset=True,
    summary="Upload a file for a comment that will reference it",
)
async def upload_comment_attachment(
    project_id: str,
    file: UploadFile = File(...),
    user: AuthenticatedUser = Depends(require_viewer),
):
    """Upload a pasted or dropped file ahead of the comment that will reference it.

    The result is ``pending``; saving a root or reply whose Markdown contains
    ``attachment:<id>`` claims it. Unclaimed uploads are swept after a day.
    """
    project = get_project_for_role_or_404(project_id, user.role)
    actor = _actor(user)
    comment_permissions.authorize(CommentAction.REPLY, actor)
    # One byte past the cap so an oversized upload is refused without reading it all.
    data = await file.read(comment_attachments.max_upload_bytes() + 1)
    try:
        prepared = await asyncio.to_thread(comment_attachments.prepare_upload, data, file.filename)
    except comment_attachments.AttachmentError as exc:
        status = 413 if exc.code == "attachment_too_large" else 415 if exc.code == "attachment_type_unsupported" else 400
        return JSONResponse(status_code=status, content={"detail": exc.detail, "code": exc.code})
    filename = comment_attachments.safe_filename(file.filename, prepared.extension)
    try:
        created = await asyncio.to_thread(
            comments_store.create_attachment,
            project.id, prepared, filename, actor.actor_id, actor.display_name,
        )
    except comment_attachments.AttachmentError as exc:
        return JSONResponse(status_code=413, content={"detail": exc.detail, "code": exc.code})
    created["url"] = f"/api/projects/{project.id}/comment-attachments/{created['id']}"
    return created


@router.get(
    "/{project_id}/comment-attachments/{attachment_id}",
    response_class=FileResponse,
    responses={
        200: {
            "content": {
                "image/png": {},
                "image/jpeg": {},
                "image/webp": {},
                "application/pdf": {},
                "application/zip": {},
                "text/plain": {},
                "text/csv": {},
                "text/markdown": {},
            },
            "description": "The stored attachment, inline for images and a download otherwise",
        }
    },
    summary="Download a comment attachment",
)
async def get_comment_attachment(
    project_id: str,
    attachment_id: str,
    user: AuthenticatedUser = Depends(require_viewer),
):
    """Serve an attachment to anyone who can read the project's comments.

    Images are re-encoded at upload and may render inline; every other type is
    a download. The sandbox CSP and ``nosniff`` stop a browser from treating
    any stored file as an active document.
    """
    project = get_project_for_role_or_404(project_id, user.role)
    row = await asyncio.to_thread(comments_store.get_attachment, project.id, attachment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    path = comment_attachments.blob_path(row["sha256"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Attachment not found")
    media_type = str(row["media_type"])
    inline = media_type.startswith("image/")
    return FileResponse(
        path,
        media_type=media_type,
        filename=row["filename"],
        content_disposition_type="inline" if inline else "attachment",
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )
