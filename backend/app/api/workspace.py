import asyncio
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Request, Response

from app.core.security import AuthenticatedUser, require_viewer
from app.services import pipeline_service
from app.services.workspace_service import workspace
from app.api._helpers import _row_to_project

router = APIRouter(dependencies=[Depends(require_viewer)])


@router.get("/bootstrap")
async def get_workspace_bootstrap(
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(require_viewer),
):
    data = await asyncio.to_thread(workspace.get_bootstrap_data, user.role)
    version = int(data.get("version") or 1)
    etag = f'"workspace-{version}-{user.role}"'
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": "private, no-cache"},
        )
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "private, no-cache"
    projects = [_row_to_project(r) for r in data["projects"]]
    return {
        "projects": projects,
        "folders": data["folders"],
        "systems": data.get("systems", []),
        "version": version,
    }


@router.get("/pipeline")
async def get_workspace_pipeline(user: AuthenticatedUser = Depends(require_viewer)):
    """Where every board stands, from the repositories that commit ``.prism-pipeline.json``.

    Only the projects the caller's role sees are tied to rows; the release sign-off
    column is read from Release Studio for those projects.
    """
    def build() -> Dict[str, Any]:
        projects = workspace.get_bootstrap_data(user.role)["projects"]
        return {"pipelines": pipeline_service.pipelines(projects, pipeline_service.default_seams())}

    return await asyncio.to_thread(build)
