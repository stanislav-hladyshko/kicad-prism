"""Fabrication viewer routes: Gerber layers, drill and pick-and-place.

Two sources feed the same view model.  A Release Studio build serves the package
it released, read digest-checked out of its stored dossier; a project's output
folders serve the Gerber and drill files it keeps, from the working tree or at a
commit.  Only where the bytes come from differs.  Mounted at ``/api/projects``.
"""

from __future__ import annotations

import logging
import os
import posixpath
import re
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from app.api import fabrication_view_models as models
from app.api._helpers import (
    SANDBOXED_CONTENT_HEADERS,
    get_project_for_role_or_404,
    require_output_type,
    resolve_path_within_root,
)
from app.api.release_studio import ARCHIVES
from app.core.security import AuthenticatedUser, require_viewer
from app.services import project_outputs
from app.services import release_studio_service as release_store
from app.services.fabrication_view_service import (
    BoundedCache,
    FabricationPackage,
    RENDER_VERSION,
    nearest_export,
)
from app.services.placement_service import (
    PlacementError,
    parse_bom,
    parse_positions,
    position_files,
)
from app.services.placement_service import build_view as build_placement_view
from app.services.placement_service import view_of as placement_view_of

router = APIRouter(dependencies=[Depends(require_viewer)])
logger = logging.getLogger(__name__)

#: Gerber (`.gbr`, KiCad's Protel `.gtl`/`.gbl`/`.gm1`/..., inner `.g1`), job file, drill.
_LAYER_FILE = re.compile(
    r"\.(gbr|gtl|gbl|gts|gbs|gto|gbo|gtp|gbp|gta|gba|gm\d|gko|g\d+|gbrjob|drl|xln)$",
    re.IGNORECASE,
)
#: A position file or BOM: CSV, or KiCad's ``*-pos`` export whatever its extension.
_TABLE_FILE = re.compile(r"(\.(csv|pos|txt)|[-_]pos)$", re.IGNORECASE)
_LAYERS = (_LAYER_FILE, "No Gerber or drill files in this folder")
_TABLES = (_TABLE_FILE, "No position file or BOM in this folder")

#: How many folder levels below the one asked for belong to its package. CAM
#: exports keep drill files in a subfolder of the Gerbers, and an output folder
#: keeps the Gerbers, the positions and the BOM in folders of their own.
_MAX_DEPTH = 3

#: Limits that keep one request bounded: how many files, how big each is, and how
#: much a folder may ask to hold in memory at once.
_MAX_FILES = 80
_MAX_FILE_BYTES = 25 * 1024 * 1024
_MAX_TOTAL_BYTES = 100 * 1024 * 1024

_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")

#: Parsed packages and placement views. Neither is cheap to build; a handful is
#: enough, because a viewer asks for one package many times in a row.
_FABRICATION_CACHE_SIZE = 4
_cache = BoundedCache(_FABRICATION_CACHE_SIZE)
_fabrication_cache = BoundedCache(_FABRICATION_CACHE_SIZE)
_placement_cache = BoundedCache(_FABRICATION_CACHE_SIZE)


# ---------------------------------------------------------------------------
# Release Studio builds
# ---------------------------------------------------------------------------

#: Where a build keeps what the viewer shows.
_FABRICATION_PREFIXES = ("fabrication/gerbers/", "fabrication/drill/")
_POSITIONS_MEMBER = "assembly/positions.csv"
_BOM_MEMBER = "assembly/bom.csv"


def _dossier_files(
    build: dict[str, Any], wanted: Callable[[str], bool], missing_detail: str
) -> dict[str, bytes]:
    """Digest-checked members whose path ``wanted`` accepts, out of the stored dossier.

    The same read as Release Studio's member download: a viewer only ever shows
    bytes that match what the manifest released.
    """

    members = [
        item
        for item in release_store.build_members(build["id"])
        if wanted(str(item["path"]))
    ]
    if not members:
        raise HTTPException(status_code=404, detail=missing_detail)
    return ARCHIVES.released_members(build, members)


def _fabrication_files(build: dict[str, Any]) -> dict[str, bytes]:
    return _dossier_files(
        build,
        lambda path: path.startswith(_FABRICATION_PREFIXES),
        "This build has no fabrication files",
    )


def _fabrication_package(build: dict[str, Any]) -> FabricationPackage:
    # A build is immutable, so its parsed package never goes stale.
    build_id = str(build["id"])
    cached = _fabrication_cache.get(build_id)
    if cached is not None:
        return cached
    package = FabricationPackage.from_files(_fabrication_files(build))
    _fabrication_cache.put(build_id, package)
    return package


def _build_for(project_id: str, build_id: str, user: AuthenticatedUser) -> dict[str, Any]:
    get_project_for_role_or_404(project_id, user.role)
    return ARCHIVES.build(project_id, build_id)


# Plain `def` throughout: parsing a dense board takes real CPU, which belongs in
# the threadpool and not on the event loop.
@router.get(
    "/{project_id}/release-studio/builds/{build_id}/fabrication-view",
    response_model=models.FabricationView,
    summary="Layers, board size and drill tools of a build's fabrication package",
)
def get_fabrication_view(
    project_id: str, build_id: str, user: AuthenticatedUser = Depends(require_viewer)
):
    """Layers, board size and drill tools of the build's fabrication package."""

    return _fabrication_package(_build_for(project_id, build_id, user)).view()


@router.get(
    "/{project_id}/release-studio/builds/{build_id}/fabrication-view/layers/{layer_id}.svg",
    response_class=Response,
    summary="One layer of a build's fabrication package as SVG",
    responses={200: {"content": {"image/svg+xml": {}}, "description": "The layer SVG."}},
)
def get_fabrication_layer(
    project_id: str,
    build_id: str,
    layer_id: str,
    user: AuthenticatedUser = Depends(require_viewer),
):
    """One layer drawn as SVG against the package's shared board extent."""

    build = _build_for(project_id, build_id, user)
    try:
        svg = _fabrication_package(build).svg(layer_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Layer not found") from exc
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "ETag": f'"{build_id}-{layer_id}-r{RENDER_VERSION}"',
            **SANDBOXED_CONTENT_HEADERS,
        },
    )


@router.get(
    "/{project_id}/release-studio/builds/{build_id}/placement",
    response_model=models.PlacementView,
    summary="Pick-and-place parts of a build, checked against its BOM",
)
def get_build_placement(
    project_id: str, build_id: str, user: AuthenticatedUser = Depends(require_viewer)
):
    """Pick-and-place parts of the build, checked against its BOM when it has one."""

    build = _build_for(project_id, build_id, user)
    cached = _placement_cache.get(("build", str(build["id"])))
    if cached is not None:
        return cached
    files = _dossier_files(
        build,
        lambda path: path in (_POSITIONS_MEMBER, _BOM_MEMBER),
        "This build has no position file",
    )
    if _POSITIONS_MEMBER not in files:
        raise HTTPException(status_code=404, detail="This build has no position file")
    bom = files.get(_BOM_MEMBER)
    view = build_placement_view(
        files[_POSITIONS_MEMBER].decode("utf-8", errors="replace"),
        bom.decode("utf-8", errors="replace") if bom is not None else None,
        positions_name=_POSITIONS_MEMBER.rsplit("/", 1)[-1],
        bom_name=_BOM_MEMBER.rsplit("/", 1)[-1],
    )
    _placement_cache.put(("build", str(build["id"])), view)
    return view


# ---------------------------------------------------------------------------
# Committed output folders (the Assets Portal)
# ---------------------------------------------------------------------------


def _clean_folder(folder: str) -> str:
    normalized = posixpath.normpath(folder.replace("\\", "/")) if folder.strip() else ""
    if normalized in ("", "."):
        return ""
    if normalized.startswith("/") or normalized == ".." or normalized.startswith("../"):
        raise HTTPException(status_code=400, detail="Invalid folder path")
    return normalized


def _candidates(names_sizes: list[tuple[str, int]], kind) -> list[tuple[str, int]]:
    """The files of one kind, with the limits that keep a request bounded.

    Layers are those of the nearest export (``nearest_export``); tables are
    every position file and BOM under the folder, wherever the export keeps them.
    """

    pattern, empty = kind
    found = [(name, size) for name, size in names_sizes if pattern.search(name)]
    if kind is _LAYERS:
        # One export, and the limits measure that export, not the whole tree.
        shown = set(nearest_export([name for name, _ in found]))
        found = [(name, size) for name, size in found if name in shown]
    if not found:
        raise HTTPException(status_code=404, detail=empty)
    if len(found) > _MAX_FILES:
        raise HTTPException(status_code=413, detail="Too many files to view as one package")
    if any(size > _MAX_FILE_BYTES for _, size in found):
        raise HTTPException(status_code=413, detail="A file in this folder is too large to view")
    if sum(size for _, size in found) > _MAX_TOTAL_BYTES:
        raise HTTPException(status_code=413, detail="This folder is too large to view as one package")
    return found


def _walk(directory: str, kind, prefix: str = "", depth: int = 0) -> list[tuple[str, os.stat_result]]:
    """Files of one kind under ``directory``, by path relative to it, ``_MAX_DEPTH`` deep.

    Hidden folders are skipped, and links are never followed: a link could
    reach outside the output folder the request was checked against.
    """

    found: list[tuple[str, os.stat_result]] = []
    for entry in os.scandir(directory):
        if entry.name.startswith("."):
            continue
        path = f"{prefix}{entry.name}"
        if entry.is_dir(follow_symlinks=False):
            if depth < _MAX_DEPTH:
                found.extend(_walk(entry.path, kind, f"{path}/", depth + 1))
        elif entry.is_file(follow_symlinks=False) and kind[0].search(entry.name):
            found.append((path, entry.stat(follow_symlinks=False)))
    return found


def _working_tree(project, output_type: str, folder: str, kind=_LAYERS):
    output_dir = project_outputs.output_dir(project, output_type)
    directory = resolve_path_within_root(output_dir, folder, invalid_detail="Invalid folder path")
    if not directory.is_dir():
        raise HTTPException(status_code=404, detail="Folder not found")
    walked = _walk(str(directory), kind)
    chosen = {path for path, _ in _candidates([(path, stat.st_size) for path, stat in walked], kind)}
    entries = [(path, stat) for path, stat in walked if path in chosen]
    signature = tuple(sorted((path, stat.st_size, stat.st_mtime_ns) for path, stat in entries))

    def read() -> dict[str, bytes]:
        return {path: (directory / path).read_bytes() for path, _ in entries}

    return signature, read


def _at_commit(project, output_type: str, folder: str, commit: str, kind=_LAYERS):
    config = project_outputs.path_config_at(project, commit)
    base = project_outputs.join_relative_paths(
        project_outputs.output_dir_in(config, output_type), folder
    )
    items = [
        item
        for item in project_outputs.list_commit_files(project, commit, base)
        if not item.is_dir
        and item.path.count("/") <= _MAX_DEPTH
        and not any(part.startswith(".") for part in item.path.split("/"))
    ]
    found = _candidates([(item.path, item.size) for item in items], kind)
    signature = tuple(sorted(found))

    def read() -> dict[str, bytes]:
        return {
            name: project_outputs.read_commit_file(
                project, commit, name, relative_prefix=base, not_found_detail="File not found"
            ).content
            for name, _ in found
        }

    return signature, read


def _folder_files(
    project_id: str,
    user: AuthenticatedUser,
    output_type: str,
    folder: str,
    commit: Optional[str],
    kind,
):
    """Signature and reader for one kind of file in a project output folder."""

    project = get_project_for_role_or_404(project_id, user.role)
    return (
        _at_commit(project, output_type, folder, commit, kind)
        if commit
        else _working_tree(project, output_type, folder, kind)
    )


def _cacheable(commit: Optional[str]) -> bool:
    """A working tree is keyed by its files' signature; a commit only if it is pinned.

    A branch or tag name can move without any file in the folder changing size,
    so a name is never a safe key.
    """

    return not commit or bool(_FULL_SHA.fullmatch(commit))


def _package(
    project_id: str,
    user: AuthenticatedUser,
    output_type: str,
    folder: str,
    commit: Optional[str],
) -> FabricationPackage:
    output_type = require_output_type(output_type)
    folder = _clean_folder(folder)
    signature, read = _folder_files(project_id, user, output_type, folder, commit, _LAYERS)
    key = (project_id, output_type, folder, commit or "", signature)
    cached = _cache.get(key) if _cacheable(commit) else None
    if cached is not None:
        return cached
    package = FabricationPackage.from_files(read())
    if _cacheable(commit):
        _cache.put(key, package)
    return package


@router.get(
    "/{project_id}/fabrication-view",
    response_model=models.FabricationView,
    summary="Layers, board size and drill tools of the Gerbers in one output folder",
)
def get_output_fabrication_view(
    project_id: str,
    type: str = "manufacturing",
    folder: str = "",
    commit: Optional[str] = None,
    user: AuthenticatedUser = Depends(require_viewer),
):
    """Layers, board size and drill tools of the Gerber files in one folder."""

    return _package(project_id, user, type, folder, commit).view()


@router.get(
    "/{project_id}/fabrication-view/layers/{layer_id}.svg",
    response_class=Response,
    summary="One layer of an output folder's package as SVG",
    responses={200: {"content": {"image/svg+xml": {}}, "description": "The layer SVG."}},
)
def get_output_fabrication_layer(
    project_id: str,
    layer_id: str,
    type: str = "manufacturing",
    folder: str = "",
    commit: Optional[str] = Query(None),
    user: AuthenticatedUser = Depends(require_viewer),
):
    """One layer of that package as SVG, on the package's shared extent."""

    package = _package(project_id, user, type, folder, commit)
    try:
        svg = package.svg(layer_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Layer not found") from exc
    pinned = bool(commit and _FULL_SHA.fullmatch(commit))
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={
            # Working-tree files can change, so only a pinned commit is immutable.
            "Cache-Control": "private, max-age=31536000, immutable" if pinned else "private, no-cache",
            **SANDBOXED_CONTENT_HEADERS,
        },
    )


def _preferred(names, prefer: re.Pattern[str]) -> list[str]:
    """``names`` with those named like ``prefer`` first, shallowest first, then by name."""

    return sorted(
        names,
        key=lambda item: (not prefer.search(item.rsplit("/", 1)[-1]), item.count("/"), item.casefold()),
    )


_POSITION_NAME = re.compile(r"pos|cpl|place|centroid", re.IGNORECASE)
_BOM_NAME = re.compile(r"bom|parts", re.IGNORECASE)


class PlacementPart(BaseModel):
    """One placed part in board coordinates."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    ref: str
    value: str
    package: str
    x: float
    y: float
    rotation: float
    side: str
    status: str


class PlacementMissingPart(BaseModel):
    """A BOM part the position files never place."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    ref: str
    value: str
    package: str
    status: str


class PlacementCounts(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    placed: int
    top: int
    bottom: int
    notInBom: int
    dnpPlaced: int
    notPlaced: int


class PlacementFiles(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    positions: str
    bom: str


class PlacementView(BaseModel):
    """The parts of a folder's position files, checked against its BOM."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    present: bool
    parts: list[PlacementPart]
    missing: list[PlacementMissingPart]
    counts: PlacementCounts
    hasBom: bool
    files: PlacementFiles
    warnings: list[str]


@router.get(
    "/{project_id}/placement",
    response_model=PlacementView,
    summary="Placed parts of a folder's position files, checked against its BOM",
)
def get_output_placement(
    project_id: str,
    type: str = "manufacturing",
    folder: str = "",
    commit: Optional[str] = None,
    user: AuthenticatedUser = Depends(require_viewer),
):
    """Parts of the position files in a folder, checked against its BOM if it has one.

    The files are found by what they contain, not by name: KiCad calls them
    ``*-pos.csv`` and ``*.csv``, the JLCPCB plugin ``CPL-*.csv`` and ``BOM-*.csv``.
    A top and a bottom position file are read together.
    """

    output_type = require_output_type(type)
    folder = _clean_folder(folder)
    signature, read = _folder_files(project_id, user, output_type, folder, commit, _TABLES)
    key = ("outputs", project_id, output_type, folder, commit or "", signature)
    cached = _placement_cache.get(key) if _cacheable(commit) else None
    if cached is not None:
        return cached
    # Each table is parsed once: as a position file if it has coordinates,
    # otherwise as a BOM. A file that reads as a position file is never a BOM.
    parsed: dict[str, tuple] = {}
    boms: dict[str, list] = {}
    for name, data in read().items():
        text = data.decode("utf-8", errors="replace")
        try:
            parsed[name] = parse_positions(text)
            continue
        except PlacementError:
            pass
        try:
            boms[name] = parse_bom(text)
        except PlacementError:
            pass
    positions = position_files([(name, parsed[name][0]) for name in _preferred(parsed, _POSITION_NAME)])
    if not positions:
        raise HTTPException(status_code=404, detail="No position file in this folder")
    bom = next(iter(_preferred(boms, _BOM_NAME)), None)
    view = placement_view_of(
        [parsed[name] for name in positions],
        boms[bom] if bom else None,
        positions_name=", ".join(positions),
        bom_name=bom or "",
    )
    if _cacheable(commit):
        _placement_cache.put(key, view)
    return view
