"""Response models for the routes of ``app/api/projects.py``.

MCP-06a typed the read routes and MCP-06f the write and job routes. Every model
describes the body the handler already returned and allows extra keys, so a
field added by a service still reaches the client; the handlers keep returning
their plain dictionaries.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict

from app.services import path_config_service, project_service


class MonorepoFolder(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    path: str
    item_count: int


class MonorepoProject(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    name: str
    display_name: Optional[str]
    relative_path: str
    has_thumbnail: bool
    last_modified: str


class MonorepoStructure(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    repo_name: str
    current_path: str
    folders: List[MonorepoFolder]
    projects: List[MonorepoProject]


class ProjectSearchHit(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    name: str
    description: str
    parent_repo: Optional[str]
    sub_path: Optional[str]
    last_modified: str
    last_synced_at: Optional[str]
    thumbnail_url: Optional[str]


class ProjectSearchResults(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    results: List[ProjectSearchHit]


class ProjectBranch(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    ref: str
    source: str
    is_current: bool
    hash: str
    commit: str
    author: str
    email: str
    date: str
    message: str


class ProjectBranches(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    branches: List[ProjectBranch]
    current_branch: Optional[str]
    default_ref: Optional[str]


class ProjectReadme(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    content: str


class DocFileContent(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    content: str
    path: str


class ProjectRelease(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    tag: str
    commit_hash: str
    full_hash: str
    date: str
    message: str
    # Only a Type-2 page counts the subproject's files a tag changed.
    subproject_files_changed: Optional[int] = None


class ProjectReleases(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    releases: List[ProjectRelease]
    total: Optional[int]
    has_more: bool
    limit: int
    offset: int
    resolved_ref_sha: Optional[str] = None


class CommitKicadChanges(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    sch: int
    pcb: int
    pro: int
    other: int


class ProjectCommit(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    hash: str
    full_hash: str
    author: str
    email: str
    date: str
    message: str
    kicad_changes: CommitKicadChanges


class ProjectCommits(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    commits: List[ProjectCommit]
    total: Optional[int]
    has_more: bool
    limit: int
    offset: int
    resolved_ref_sha: str
    head: Optional[str]


class CommitDistance(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    commits_behind: int


class CommitFileChange(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    path: str
    filename: str
    status: str
    additions: Optional[int]
    deletions: Optional[int]


class ProjectCommitSummary(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    files: List[CommitFileChange]
    base_commit: Optional[str]
    compare_commit: str
    parent_count: int
    comparison_basis: str


class ProjectPathConfig(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    config: path_config_service.PathConfig
    resolved: path_config_service.ResolvedPaths
    source: str


class ProjectName(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    display_name: Optional[str]
    fallback_name: str


class ProjectDescription(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    description: str


class ProjectOverview(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    project: project_service.Project
    readme: Optional[str]


class JobStarted(BaseModel):
    """A route that queued a job and reported the status the service chose."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    job_id: str
    status: str


class SyncQueued(JobStarted):
    """The sync route adds how the queueing went, not just the id."""

    message: str


class WorkflowQueued(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    job_id: str


class ImportAccessHelp(BaseModel):
    """What to do about a repository Prism cannot read."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    forge: str
    deploy_key_url: Optional[str]
    account_key_url: Optional[str]
    instructions: str
    public_key: Optional[str]
    fingerprint: Optional[str]
    key_exists: bool
    host: str
    host_trusted: bool


class ThumbnailQueued(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    status: str
    job_id: str
    message: str


class ThumbnailsQueued(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    status: str
    job_ids: List[str]
    count: int
    message: str


class ThumbnailUploaded(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    status: str
    thumbnail_source: str
    message: str


class ThumbnailCleared(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    status: str
    thumbnail_source: str
    # Absent the render is already there; present, one was queued to replace it.
    job_id: Optional[str]
    message: str


class ProjectDeleted(BaseModel):
    """A removed project and what happened to its files and repository."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    message: str
    removed_files: bool
    repository_deleted: bool
    # Only set when part of the deletion could not complete.
    warning: Optional[str] = None


class PathConfigValidation(BaseModel):
    """A path configuration checked against the project on disk."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    valid: bool
    errors: List[str]
    warnings: List[str]
    # Field name to the absolute path it resolved to.
    resolved: Dict[str, str]


class DetectedPaths(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    detected: path_config_service.PathConfig
    validation: PathConfigValidation


class ProjectConfigSaved(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    config: path_config_service.PathConfig
    resolved: path_config_service.ResolvedPaths
    validation: PathConfigValidation


class ProjectNameUpdated(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    display_name: str
    message: str


class ProjectDescriptionUpdated(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    description: str
    message: str


__all__ = [
    "CommitDistance",
    "CommitFileChange",
    "CommitKicadChanges",
    "DetectedPaths",
    "DocFileContent",
    "ImportAccessHelp",
    "JobStarted",
    "MonorepoFolder",
    "MonorepoProject",
    "MonorepoStructure",
    "PathConfigValidation",
    "ProjectBranch",
    "ProjectBranches",
    "ProjectCommit",
    "ProjectCommitSummary",
    "ProjectCommits",
    "ProjectConfigSaved",
    "ProjectDeleted",
    "ProjectDescription",
    "ProjectDescriptionUpdated",
    "ProjectName",
    "ProjectNameUpdated",
    "ProjectOverview",
    "ProjectPathConfig",
    "ProjectReadme",
    "ProjectRelease",
    "ProjectReleases",
    "ProjectSearchHit",
    "ProjectSearchResults",
    "SyncQueued",
    "ThumbnailCleared",
    "ThumbnailQueued",
    "ThumbnailUploaded",
    "ThumbnailsQueued",
    "WorkflowQueued",
]


class CommentsSourceRelativeUrls(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    list_url: str
    patch_url_template: str
    reply_url_template: str
    delete_url_template: str


class ProjectCommentsSourceUrls(BaseModel):
    """The helper URLs a KiCad comments source needs for one project."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    project_id: str
    project_name: str
    base_url: Optional[str]
    list_url: str
    patch_url_template: str
    reply_url_template: str
    delete_url_template: str
    relative: CommentsSourceRelativeUrls
