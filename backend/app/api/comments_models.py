"""Response models for the comments API (ADR scripting-and-mcp §3.2).

They describe the JSON the store already projects: ``comments_store_codec``
shapes a root and its replies, ``comment_capabilities`` adds ``permissions``,
``comment_social`` adds ``reactions``/``editedAt``, ``comment_attachments``
adds ``attachments`` and ``trackers.projections`` adds ``tracker``. Every model
allows extras so a key these models miss still reaches the client, and the
routes serialize with ``response_model_exclude_unset`` so an absent optional
key stays absent instead of becoming ``null``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _CommentModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class CommentLocationView(_CommentModel):
    x: float
    y: float
    layer: str
    page: str
    bounds: Optional[List[float]] = None


class CommentAnchorView(_CommentModel):
    state: str
    source: Optional[str]
    commit: Optional[str]
    sourceRevisionKey: Optional[str]
    designKey: Optional[str]
    baseCommit: Optional[str]
    compareCommit: Optional[str]
    selectedSide: Optional[str]
    projectFile: Optional[str] = None


class CommentPermissionsView(_CommentModel):
    canReply: Optional[bool] = None
    canEdit: Optional[bool] = None
    canDelete: Optional[bool] = None
    canResolve: Optional[bool] = None
    canPublish: Optional[bool] = None
    canRetry: Optional[bool] = None
    canShare: Optional[bool] = None


class CommentReactionView(_CommentModel):
    reaction: str
    count: int
    users: List[str]
    mine: bool = False


class CommentAttachmentView(_CommentModel):
    id: str
    filename: str
    mediaType: str
    size: int
    state: str
    width: Optional[int] = None
    height: Optional[int] = None


class CommentReplySyncView(_CommentModel):
    state: str
    reason: Optional[str] = None
    externalCommentId: Optional[str] = None
    externalUrl: Optional[str] = None


class CommentReplyView(_CommentModel):
    id: str
    author: str
    authorUserId: Optional[str]
    authorKind: str
    authorClientId: Optional[str]
    #: The name the client declared when it was approved ("Claude Code").
    authorClientName: Optional[str] = None
    timestamp: str
    updatedAt: str
    revision: int
    content: str
    contentFormat: str
    origin: str
    deletedAt: Optional[str] = None
    syncState: Optional[str] = None
    reactions: Optional[List[CommentReactionView]] = None
    editedAt: Optional[str] = None
    attachments: Optional[List[CommentAttachmentView]] = None
    permissions: Optional[CommentPermissionsView] = None
    sync: Optional[CommentReplySyncView] = None


class TrackerDestinationView(_CommentModel):
    connectorId: str
    containerPath: str
    containerKind: str
    generation: int


class TrackerProviderErrorView(_CommentModel):
    # ``class`` is a Python keyword, so the wire name is carried as an alias.
    syncClass: Optional[str] = Field(default=None, alias="class")
    message: Optional[str] = None
    resumeAt: Optional[Any] = None
    status: Optional[Any] = None
    retryable: Optional[bool] = None
    newRef: Optional[Any] = None


class TrackerProjectionView(_CommentModel):
    linkState: Optional[str] = None
    promoteMinRole: Optional[str] = None
    provider: Optional[str] = None
    notPromotableReason: Optional[str] = None
    externalId: Optional[str] = None
    externalNumber: Optional[str] = None
    externalUrl: Optional[str] = None
    destination: Optional[TrackerDestinationView] = None
    remoteState: Optional[Any] = None
    remoteUpdatedAt: Optional[str] = None
    pendingIntent: Optional[str] = None
    syncState: Optional[str] = None
    pausedReason: Optional[str] = None
    bodyAuthority: Optional[str] = None
    lastError: Optional[TrackerProviderErrorView] = None


class CommentView(_CommentModel):
    """One root thread, whether read or returned by a write."""

    id: str
    author: str
    authorUserId: Optional[str]
    authorKind: str
    authorClientId: Optional[str]
    #: The name the client declared when it was approved ("Claude Code").
    authorClientName: Optional[str] = None
    timestamp: str
    updatedAt: str
    revision: int
    status: str
    context: str
    location: CommentLocationView
    content: str
    contentFormat: str
    replies: List[CommentReplyView]
    commentClass: str
    severity: str
    mentions: List[str]
    anchor: CommentAnchorView
    scope: str
    deletedAt: Optional[str] = None
    elementId: Optional[str] = None
    elementRef: Optional[str] = None
    elementType: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    baseCommit: Optional[str] = None
    compareCommit: Optional[str] = None
    comparisonDomain: Optional[str] = None
    filePath: Optional[str] = None
    semanticItemId: Optional[str] = None
    anchorKind: Optional[str] = None
    forgeProvider: Optional[str] = None
    forgeIssueId: Optional[str] = None
    forgeIssueUrl: Optional[str] = None
    forgeSyncState: Optional[str] = None
    reactions: Optional[List[CommentReactionView]] = None
    editedAt: Optional[str] = None
    attachments: Optional[List[CommentAttachmentView]] = None
    tracker: Optional[TrackerProjectionView] = None
    permissions: Optional[CommentPermissionsView] = None


class CommentsMetaView(_CommentModel):
    version: str
    generator: str


class CommentsListing(_CommentModel):
    """A snapshot listing (canvas or one comparison pair)."""

    meta: CommentsMetaView
    comments: List[CommentView]
    cursor: int


class CommentThreadView(_CommentModel):
    comment: Optional[CommentView]
    cursor: int


class ReplyCreatedView(_CommentModel):
    comment: CommentView
    reply: CommentReplyView


class DeletedCommentView(_CommentModel):
    deleted: str


class CommentHistoryEntry(_CommentModel):
    targetKind: str
    targetId: str
    revision: int
    changeKind: str
    content: Optional[str]
    contentFormat: Optional[str]
    severity: Optional[str]
    commentClass: Optional[str]
    status: Optional[str]
    mentions: Optional[List[str]]
    editorUserId: Optional[str]
    editorKind: str
    editorDisplay: str
    editorAuthType: Optional[str]
    editorClientId: Optional[str]
    origin: str
    editedAt: str


class PushCommentsView(_CommentModel):
    success: bool
    message: str
    comments_path: str


class CommentAttachmentUploadView(CommentAttachmentView):
    url: str
