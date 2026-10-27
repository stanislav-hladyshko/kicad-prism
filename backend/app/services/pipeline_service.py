"""Pipeline: where every board of a repository stands, from a file the repository commits.

A repository that runs its own design pipeline (gates, simulations, comparison with a
reference board) writes the result to ``.prism-pipeline.json`` at its root. Prism does
not judge the stages; it reads the file from the checkout, ties each row to the Prism
project at the same path, and adds the one column only Prism knows: the release
sign-off (Designer and QA approvals of a Release Studio build).

File contract, version 1::

    {
      "version": 1,
      "as_of": "2026-10-26",                     # last gate run, set by the repository
      "columns": ["1. Вимоги", ..., "Еталон"],   # stage names, in order
      "rows": [{
        "id": "G4", "dir": "examples/golden/G4", "board": "...", "kind": "...",
        "stop": "6. Розміщення",                 # first stage that stops the board
        "cells": [{"state": "ok|warn|bad|human|none|na", "text": "...", "why": "..."}]
      }]
    }

A row whose ``dir`` has no Prism project (not imported yet, or no board yet) is still
listed, without a project id.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

PIPELINE_FILE = ".prism-pipeline.json"
SUPPORTED_VERSION = 1
STATES = {"ok", "warn", "bad", "human", "none", "na"}


@dataclass(frozen=True)
class Signoff:
    state: str
    text: str
    why: str

    def as_cell(self) -> dict[str, str]:
        return {"state": self.state, "text": self.text, "why": self.why}


WAITING = Signoff("human", "чекає", "у Prism немає збірки релізу з підписами Designer і QA")


@dataclass
class Seams:
    """What the service reads; tests pass fakes, the router passes ``default_seams()``."""

    read_file: Callable[[Path], Optional[str]]
    list_candidates: Callable[[str], list[Mapping[str, Any]]]
    list_builds: Callable[[str], list[Mapping[str, Any]]]
    latest_review_decision: Callable[[str, str], Optional[Mapping[str, Any]]]
    commit_distance: Callable[[str, str, Optional[str]], int]


def default_seams() -> Seams:
    from app.services import git_service
    from app.services import release_studio_service as store

    def read_file(path: Path) -> Optional[str]:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    return Seams(
        read_file=read_file,
        list_candidates=lambda project_id: store.list_candidates(project_id),
        list_builds=store.list_builds,
        latest_review_decision=store.latest_review_decision,
        commit_distance=lambda repo, commit, sub: git_service.get_commit_distance(repo, commit, sub),
    )


def _active(decision: Optional[Mapping[str, Any]], digest: str) -> Optional[Mapping[str, Any]]:
    # the rule of release_studio_service._active_approval: an approval of this very dossier
    if not decision or decision.get("decision") != "approved":
        return None
    return decision if str(decision.get("dossier_digest") or "") == digest else None


def _date(value: Any) -> str:
    return value.date().isoformat() if hasattr(value, "date") else str(value)[:10]


def release_signoff(project_id: str, repo_root: str, sub_path: Optional[str], seams: Seams) -> Signoff:
    """The newest candidate whose latest build Designer and QA both approved, against the board's HEAD."""

    for candidate in seams.list_candidates(project_id):  # newest first
        builds = seams.list_builds(str(candidate["id"]))
        build = builds[0] if builds else None
        if not build or build.get("status") != "succeeded":
            continue
        digest = str(build.get("dossier_digest") or "")
        designer = _active(seams.latest_review_decision(str(build["id"]), "designer"), digest)
        qa = _active(seams.latest_review_decision(str(build["id"]), "qa"), digest)
        if not (designer and qa):
            continue
        date = max(_date(designer["created_at"]), _date(qa["created_at"]))
        commit = str(candidate["commit_sha"])
        behind = seams.commit_distance(repo_root, commit, sub_path)
        if behind == 0:
            return Signoff("ok", f"підписано {date}", f"Designer і QA підписали ревізію {commit[:7]}")
        return Signoff("warn", "підпис на старій ревізії",
                       f"підписано {date} на {commit[:7]}, після неї плату змінено {behind} комітами")
    return WAITING


def _clean_cell(cell: Any) -> dict[str, str]:
    if not isinstance(cell, Mapping):
        return {"state": "na", "text": "-", "why": ""}
    state = str(cell.get("state") or "na")
    return {"state": state if state in STATES else "na", "text": str(cell.get("text") or "-"),
            "why": str(cell.get("why") or "")}


def read_pipeline(text: str) -> Optional[dict[str, Any]]:
    """The parsed file, or None when it is not a version this Prism understands."""

    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        logger.warning("Unreadable %s: %s", PIPELINE_FILE, err)
        return None
    if not isinstance(data, dict) or data.get("version") != SUPPORTED_VERSION:
        logger.warning("Unsupported %s version: %r", PIPELINE_FILE, data.get("version") if isinstance(data, dict) else None)
        return None
    return data


def pipelines(projects: Iterable[Mapping[str, Any]], seams: Seams) -> list[dict[str, Any]]:
    """One pipeline per repository that commits the file, limited to the projects the caller sees.

    ``projects`` are workspace rows already filtered by the caller's role (``repo_id``,
    ``parent_repo_path``, ``parent_repo``, ``relative_path``, ``id``).
    """

    by_repo: dict[str, list[Mapping[str, Any]]] = {}
    for project in projects:
        if project.get("parent_repo_path"):
            by_repo.setdefault(str(project["repo_id"]), []).append(project)

    result = []
    for repo_id, repo_projects in by_repo.items():
        root = str(repo_projects[0]["parent_repo_path"])
        text = seams.read_file(Path(root) / PIPELINE_FILE)
        data = read_pipeline(text) if text is not None else None
        if data is None:
            continue
        by_dir = {str(p.get("relative_path") or ".").strip("/"): p for p in repo_projects}
        rows = []
        for row in data.get("rows") or []:
            board_dir = str(row.get("dir") or "").strip("/")
            project = by_dir.get(board_dir) if board_dir else None
            if project:
                human = release_signoff(str(project["id"]), root, board_dir, seams)
            elif board_dir:
                human = Signoff("human", "чекає", "у Prism ще не імпортовано: реліз підписують тут")
            else:
                human = Signoff("none", "-", "плати ще немає")
            rows.append({
                "id": str(row.get("id") or board_dir),
                "dir": board_dir or None,
                "project_id": str(project["id"]) if project else None,
                "board": str(row.get("board") or ""),
                "kind": str(row.get("kind") or ""),
                "stop": str(row.get("stop") or ""),
                "cells": [_clean_cell(cell) for cell in row.get("cells") or []],
                "human": human.as_cell(),
            })
        result.append({
            "repo_id": repo_id,
            "repo_name": str(repo_projects[0].get("parent_repo") or repo_id),
            "as_of": str(data.get("as_of") or ""),
            "columns": [str(name) for name in data.get("columns") or []],
            "rows": rows,
        })
    return result
