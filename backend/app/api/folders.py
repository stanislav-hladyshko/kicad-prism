import asyncio
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import value_errors_as
from app.core.security import AuthenticatedUser, require_designer, require_viewer
from app.services import project_service
from app.services.workspace_service import workspace
from app.api._helpers import _row_to_project

router = APIRouter(dependencies=[Depends(require_viewer)])


class FolderNode(BaseModel):
    """One row of the folder tree the workspace renders."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    name: str
    parent_id: Optional[str]
    depth: int
    has_children: bool
    direct_project_count: int
    total_project_count: int
    visibility_mode: Optional[str]
    allowed_roles: List[str]


class FolderRecord(BaseModel):
    """A ``ws_folders`` row as the folder-content route returns it."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    name: str
    parent_id: Optional[str]
    visibility_mode: Optional[str]
    allowed_roles: List[str]
    created_at: str
    updated_at: str


class FolderContents(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    folders: List[FolderRecord]
    projects: List[project_service.Project]



class CreateFolderRequest(BaseModel):
    name: str = Field(min_length=1)
    parent_id: Optional[str] = None


class MoveProjectRequest(BaseModel):
    folder_id: Optional[str] = None


class MoveProjectsRequest(BaseModel):
    project_ids: List[str] = Field(min_length=1)
    folder_id: Optional[str] = None


class UpdateFolderRequest(BaseModel):
    name: Optional[str] = None
    parent_id: Optional[str] = None


class FolderDeleted(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    message: str


class FolderMoveResult(FolderDeleted):
    """One project moved to a folder."""


class FolderBulkMoveResult(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    message: str
    moved: int


def _normalize_folder_name(name: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="Folder name cannot be empty")
    return normalized


@router.get("/tree", response_model=List[FolderNode], summary="The folder tree the workspace renders")
async def get_folder_tree(user: AuthenticatedUser = Depends(require_viewer)):
    return await asyncio.to_thread(workspace.get_folder_tree, user.role)


@router.get("/contents", response_model=FolderContents, summary="Folders and projects directly inside one folder")
async def get_folder_contents(
    folder_id: Optional[str] = Query(default=None),
    user: AuthenticatedUser = Depends(require_viewer),
):
    with value_errors_as(404):
        payload = await asyncio.to_thread(workspace.get_folder_contents, folder_id, user.role)
        payload["projects"] = [_row_to_project(r) for r in payload["projects"]]
        return payload


@router.post(
    "/",
    dependencies=[Depends(require_designer)],
    response_model=FolderRecord,
    summary="Create a folder",
)
async def create_folder(request: CreateFolderRequest):
    with value_errors_as(400):
        return await asyncio.to_thread(
            workspace.create_folder,
            name=_normalize_folder_name(request.name),
            parent_id=request.parent_id,
        )


@router.patch(
    "/{folder_id}",
    dependencies=[Depends(require_designer)],
    response_model=FolderRecord,
    summary="Rename or reparent a folder",
)
async def update_folder(folder_id: str, request: UpdateFolderRequest):
    field_set = request.model_fields_set
    if "name" not in field_set and "parent_id" not in field_set:
        raise HTTPException(status_code=400, detail="No update fields provided")

    name = _normalize_folder_name(request.name) if "name" in field_set and request.name is not None else request.name
    use_parent = "parent_id" in field_set

    with value_errors_as(400, keep_typed=True):
        return await asyncio.to_thread(
            workspace.update_folder,
            folder_id=folder_id,
            name=name,
            parent_id=request.parent_id,
            _use_parent=use_parent,
        )


@router.delete(
    "/{folder_id}",
    dependencies=[Depends(require_designer)],
    response_model=FolderDeleted,
    summary="Delete a folder",
)
async def delete_folder(folder_id: str, cascade: bool = Query(default=True)):
    with value_errors_as(400):
        deleted = await asyncio.to_thread(workspace.delete_folder, folder_id, cascade)

    if not deleted:
        raise HTTPException(status_code=404, detail="Folder not found")

    return {"message": "Folder deleted successfully"}


@router.post(
    "/projects/{project_id}/move",
    dependencies=[Depends(require_designer)],
    response_model=FolderMoveResult,
    summary="Move one project to a folder",
)
async def move_project_to_folder(project_id: str, request: MoveProjectRequest):
    with value_errors_as(400, keep_typed=True):
        result = await asyncio.to_thread(workspace.move_project_to_folder, project_id, request.folder_id)
    if not result:
        raise HTTPException(status_code=404, detail="Project not found")

    return {"message": "Project moved successfully"}


@router.post(
    "/projects/move",
    dependencies=[Depends(require_designer)],
    response_model=FolderBulkMoveResult,
    summary="Move several projects to a folder in one transaction",
)
async def move_projects_to_folder(request: MoveProjectsRequest):
    with value_errors_as(400, keep_typed=True):
        moved = await asyncio.to_thread(
            workspace.move_projects_to_folder,
            request.project_ids,
            request.folder_id,
        )

    noun = "project" if moved == 1 else "projects"
    return {"message": f"{moved} {noun} moved successfully", "moved": moved}
