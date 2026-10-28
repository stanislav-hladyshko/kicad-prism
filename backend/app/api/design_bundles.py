"""Design-bundle routes of /api/projects (K2b).

Two routes: what a project's bundles look like, and the manual regenerate that
drops them and rebuilds without reuse. The domain behavior is
``app.services.design_bundle.regenerate``; this module only translates.
"""

from __future__ import annotations

import asyncio
import re
from typing import Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.api._helpers import get_project_for_role_or_404
from app.core.errors import caller_errors
from app.core.security import AuthenticatedUser, require_designer, require_viewer
from app.services.design_bundle import regenerate, rows, store
from app.services.design_bundle.manifest import incomplete_stages
from app.services.design_bundle.cli import CliUnavailable

router = APIRouter(prefix="/api/projects", tags=["design-bundles"])

#: The bundle states the status route always reports, even at zero.
_BUNDLE_STATES = ("complete", "building", "queued", "failed")

_COMMIT = re.compile(r"[0-9a-f]{40}")

#: How many incomplete revisions the status route lists, newest first.
_INCOMPLETE_LISTED = 20


class RegenerateRequest(BaseModel):
    """What to rebuild: one commit (default ``HEAD``) or the whole project."""

    commit: Optional[str] = None
    scope: Literal["commit", "project"] = "commit"

    @field_validator("commit")
    @classmethod
    def _commit_is_a_sha(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _COMMIT.fullmatch(value):
            raise ValueError("commit must be a 40-character lowercase hex SHA")
        return value

    @model_validator(mode="after")
    def _scope_has_no_commit(self) -> "RegenerateRequest":
        if self.commit is not None and self.scope == "project":
            raise ValueError("commit cannot be combined with scope 'project'")
        return self


class BundleCounts(BaseModel):
    """How many of the project's bundles sit in each state."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    complete: int
    building: int
    queued: int
    failed: int


class IncompleteCounts(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    failed: int
    partial: int


class IncompleteRevision(BaseModel):
    """A complete bundle whose stages failed or were only partial."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    commit: Optional[str]
    revisionKey: Optional[str]
    failedStages: list[str]
    partialStages: list[str]
    reason: str


class FailedBuild(BaseModel):
    """A bundle whose build failed outright."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    commit: Optional[str]
    revisionKey: Optional[str]
    reason: str


class DesignBundleStatus(BaseModel):
    """The bundle counts, alias count, stored size and the incomplete list."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    bundles: dict[str, int]
    aliases: int
    storedBytes: int
    incomplete: IncompleteCounts
    incompleteRevisions: list[IncompleteRevision]
    failedBuilds: list[FailedBuild]


class RegeneratedBundles(BaseModel):
    """What a manual regenerate dropped, and the rebuild it queued."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    scope: str
    commit: Optional[str]
    removedBundles: int
    removedAliases: int
    jobId: Optional[str]


@router.get(
    "/{project_id}/design-bundles",
    response_model=DesignBundleStatus,
    summary="The project's design-bundle counts, aliases and stored size",
)
async def get_design_bundles(
    project_id: str,
    user: AuthenticatedUser = Depends(require_viewer),
):
    """The project's bundle counts, alias count and stored size."""

    get_project_for_role_or_404(project_id, user.role)

    bundles = await asyncio.to_thread(rows.bundles, project_id)
    aliases = await asyncio.to_thread(rows.aliases, project_id)
    stored_bytes = await asyncio.to_thread(store.stored_bytes, project_id)

    counts = {state: 0 for state in _BUNDLE_STATES}
    for bundle in bundles:
        state = str(bundle.get("state", ""))
        if state in counts:
            counts[state] += 1
    incomplete = _incomplete(bundles)

    return {
        "bundles": counts,
        "aliases": len(aliases),
        "storedBytes": stored_bytes,
        "incomplete": {
            "failed": sum(1 for item in incomplete if item["failedStages"]),
            "partial": sum(1 for item in incomplete if not item["failedStages"]),
        },
        "incompleteRevisions": incomplete[:_INCOMPLETE_LISTED],
        "failedBuilds": _failed_builds(bundles)[:_INCOMPLETE_LISTED],
    }


def _failed_builds(bundles: list[dict]) -> list[dict]:
    """Bundles whose build failed outright, newest first, with the job's error."""

    found = []
    for bundle in sorted(bundles, key=lambda row: str(row.get("updated_at") or ""), reverse=True):
        if bundle.get("state") != "failed":
            continue
        found.append({
            "commit": bundle.get("commit_sha"),
            "revisionKey": bundle.get("revision_key"),
            "reason": str(bundle.get("error") or ""),
        })
    return found


def _incomplete(bundles: list[dict]) -> list[dict]:
    """Complete bundles with a failed or partial stage, newest first, derived from their stages."""

    found = []
    for bundle in sorted(bundles, key=lambda row: str(row.get("updated_at") or ""), reverse=True):
        if bundle.get("state") != "complete":
            continue
        stages = incomplete_stages(bundle.get("stages") or {})
        if stages["failed"] or stages["partial"]:
            found.append({
                "commit": bundle.get("commit_sha"),
                "revisionKey": bundle.get("revision_key"),
                "failedStages": stages["failed"],
                "partialStages": stages["partial"],
                # The failed stages' errors, recorded when the stage was
                # published; empty for a bundle built before that.
                "reason": str(bundle.get("error") or ""),
            })
    return found


@router.post(
    "/{project_id}/design-bundles/regenerate",
    dependencies=[Depends(require_designer)],
    response_model=RegeneratedBundles,
    summary="Drop the bundles in scope and queue a rebuild that skips reuse",
)
async def regenerate_design_bundles(
    project_id: str,
    request: RegenerateRequest,
    user: AuthenticatedUser = Depends(require_viewer),
):
    """Drop the bundles in scope and queue a rebuild that skips reuse."""

    get_project_for_role_or_404(project_id, user.role)

    # The service refuses with built-in exceptions written for the caller; the
    # bridge maps each to its status without the route building a body.
    with caller_errors(
        (regenerate.RegenerateConflict, 409),
        (regenerate.RegenerateNotApplicable, 422),
        (LookupError, 404),
        (CliUnavailable, 503),
    ):
        result = await asyncio.to_thread(
            regenerate.regenerate,
            project_id,
            commit=request.commit,
            scope=request.scope,
            requested_by=user.email,
        )
    return result.to_dict()
