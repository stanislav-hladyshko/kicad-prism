"""Pipeline stage reruns of /api/projects: start the repository's stage workflow, read how its runs went.

The domain behaviour is ``app.services.pipeline_rerun_service``; this module only
finds the board of the project and translates errors.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.core.security import AuthenticatedUser, require_designer, require_viewer
from app.services import pipeline_rerun_service as reruns
from app.services import pipeline_service
from app.services.workspace_service import workspace

router = APIRouter(prefix="/api/projects", tags=["pipeline"])

NOT_CONFIGURED = "Запуск агента не налаштовано"


def _board(project_id: str, user: AuthenticatedUser) -> str:
    projects = workspace.get_bootstrap_data(user.role)["projects"]
    board = pipeline_service.board_id(projects, project_id, pipeline_service.default_seams().read_file)
    if board is None:
        raise HTTPException(status_code=404, detail="This project has no row in the repository's pipeline file.")
    return board


@router.get("/{project_id}/pipeline/reruns")
async def get_stage_reruns(project_id: str, user: AuthenticatedUser = Depends(require_viewer)) -> dict[str, Any]:
    """Whether stages can be redone here, and the latest run of each stage that still needs attention."""

    def build() -> dict[str, Any]:
        board = _board(project_id, user)
        config = reruns.config_from_settings()
        if not config.configured:
            return {"configured": False, "reason": NOT_CONFIGURED, "stages": {}}
        try:
            stages = reruns.stage_runs(board, config, reruns.github_http(config))
        except reruns.RerunError as err:
            raise HTTPException(status_code=err.status_code, detail=str(err)) from err
        return {"configured": True, "reason": None, "stages": {str(n): run for n, run in stages.items()}}

    return await asyncio.to_thread(build)


@router.post("/{project_id}/pipeline/stages/{stage}/rerun", status_code=202)
async def rerun_stage(project_id: str, stage: int, user: AuthenticatedUser = Depends(require_designer)) -> dict[str, Any]:
    """Ask the repository's workflow to redo one stage; the result comes back as a pull request."""

    def dispatch() -> dict[str, Any]:
        board = _board(project_id, user)
        config = reruns.config_from_settings()
        if not config.configured:
            raise HTTPException(status_code=503, detail=NOT_CONFIGURED)
        try:
            reruns.start(board, stage, config, reruns.github_http(config))
        except reruns.RerunError as err:
            raise HTTPException(status_code=err.status_code, detail=str(err)) from err
        return {"board": board, "stage": stage, "state": "queued"}

    return await asyncio.to_thread(dispatch)
