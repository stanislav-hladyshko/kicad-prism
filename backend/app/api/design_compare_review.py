"""A finished comparison's review queue (ADR scripting-and-mcp §3.8).

The Design Comparison workspace groups parser events into the review rows a
person accepts. An agent reading the same comparison must see those same rows,
so this route serves the server's port of that grouping
(`app/services/design_query/review_groups.py`) over ``fixtures/review-groups/``.

The result is read the way ``GET /{project_id}/design-compare/{job_id}`` reads
it: the job's published result, which is either the inline
``prism.design_compare.result.a1`` document or a manifest whose sidecars carry
its schematic, board and BOM tabs. Grouping runs in a worker thread; it walks
every change in the comparison.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api._helpers import get_project_for_role_or_404
from app.core.security import AuthenticatedUser, require_viewer
from app.services import design_compare_service
from app.services.design_query.index import natural_key
from app.services.design_query.review_groups import review_groups
from app.services.job_artifact_service import job_artifacts

router = APIRouter(dependencies=[Depends(require_viewer)])

RESULT_SCHEMA = "prism.design_compare.result.a1"
V2_MANIFEST_SCHEMA = "prism.design_compare_bundle_v2"
V1_MANIFEST_SCHEMA = "prism.design_compare_bundle_v1"

#: The manifest tabs the review queue reads. Other tabs are not its evidence.
REVIEW_SIDECARS = ("schematic", "pcb", "bom")


class ReviewGroupOut(BaseModel):
    id: str
    kind: str
    title: str
    summary: str
    impact: str
    section: str
    changeIds: list[str]
    #: Designators of the group's changes, read from each change's
    #: ``reference``. Net, track and rule groups name none.
    references: list[str] = []


class ComparisonReviewOut(BaseModel):
    jobId: str
    domain: Literal["sch", "pcb"]
    groups: list[ReviewGroupOut]


_DOMAIN_TABS = {"sch": "schematic", "pcb": "pcb"}


def with_references(groups: list[dict[str, Any]], result: dict[str, Any], domain: str) -> list[dict[str, Any]]:
    """Each group plus the designators its changes carry, in natural order."""

    changes = ((result.get(_DOMAIN_TABS[domain]) or {}).get("changes")) or []
    reference_of = {
        str(change.get("id")): str(change["reference"])
        for change in changes
        if isinstance(change, dict) and change.get("reference")
    }
    return [
        {
            **group,
            "references": sorted(
                {reference_of[change_id] for change_id in group.get("changeIds") or [] if change_id in reference_of},
                key=natural_key,
            ),
        }
        for group in groups
    ]


def _read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read_sidecar(job_id: str, metadata: dict[str, Any]) -> Optional[Any]:
    """One manifest sidecar, resolved through the job's artifact registry."""

    digest = str(metadata.get("digest") or "")
    if not digest:
        return None
    artifact = design_compare_service.get_job_sidecar(job_id, digest)
    if not artifact:
        return None
    path = job_artifacts.store.contains(str(artifact.get("object_path") or ""))
    if path is None or not path.is_file():
        return None
    return _read_json(path)


def load_comparison_result(job_id: str) -> Optional[dict[str, Any]]:
    """The review document for a job, inline or assembled from its manifest.

    ``None`` means the job's published result is not readable yet. The route
    answers that as ``comparison-not-ready``; it is not an unknown job.
    """

    result = design_compare_service.get_job_result(job_id)
    if not result:
        return None
    schema = result.get("schema")
    if schema == RESULT_SCHEMA:
        return result
    if schema not in (V2_MANIFEST_SCHEMA, V1_MANIFEST_SCHEMA):
        return None
    sidecars = result.get("sidecars") or {}
    assembled: dict[str, Any] = {}
    for name in REVIEW_SIDECARS:
        metadata = sidecars.get(name)
        if not metadata:
            return None
        payload = _read_sidecar(job_id, metadata)
        if payload is None:
            return None
        assembled[name] = payload
    return assembled


@router.get(
    "/{project_id}/design-compare/{job_id}/review",
    response_model=ComparisonReviewOut,
)
async def get_comparison_review(
    project_id: str,
    job_id: str,
    domain: Literal["sch", "pcb"] = Query(...),
    user: AuthenticatedUser = Depends(require_viewer),
) -> ComparisonReviewOut:
    """The review groups for one domain of a finished comparison."""

    get_project_for_role_or_404(project_id, user.role)
    status = await asyncio.to_thread(design_compare_service.get_job_status, job_id)
    if not status or status.get("project_id") != project_id:
        raise HTTPException(status_code=404, detail="Job not found")
    if status.get("status") != "completed":
        raise HTTPException(status_code=409, detail="comparison-not-ready")
    result = await asyncio.to_thread(load_comparison_result, job_id)
    if not result:
        raise HTTPException(status_code=409, detail="comparison-not-ready")
    groups = await asyncio.to_thread(review_groups, result, domain)
    groups = with_references(groups, result, domain)
    return ComparisonReviewOut(jobId=job_id, domain=domain, groups=groups)
