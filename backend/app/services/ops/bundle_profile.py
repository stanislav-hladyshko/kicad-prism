"""One design bundle build's profile: where its time and memory went.

A bundle job records each ``prism-kicad`` invocation and each KiCad step with
its exit, elapsed time and peak RSS, plus the upgrade/refill/GLB cache results
(``design_bundle/jobs.py``). This module finds the bundle a project and commit
resolved to, reads that job's ``result_metadata``, and returns the numbers an
operator asks for ("why did this bundle take four minutes?").

It never returns the metadata itself. ``invocations`` are reduced to the
executable's basename and one subcommand, the metadata's ``stderr`` is left
out, the bundle's own error text is redacted, and nothing here touches design
content (ADR §11, D22).
"""

from __future__ import annotations

import os
import re
import shlex
from datetime import datetime
from typing import Any

from app.core.redaction import redact_text
from app.services.design_bundle import manifest as manifest_rules
from app.services.design_bundle import rows
from app.services.design_query import revision as design_revision
from app.services.job_service import jobs
from app.services.workspace_service import workspace

_EXECUTABLE_SUFFIXES = (".exe", "-cli", "prism-kicad")


def _project_id(project: Any) -> str:
    direct = getattr(project, "id", None)
    if direct is None and isinstance(project, dict):
        direct = project.get("id")
    return str(direct or "")


def _seconds(start: Any, end: Any) -> float | None:
    try:
        first = datetime.fromisoformat(str(start).replace("Z", "+00:00")) if start else None
        last = datetime.fromisoformat(str(end).replace("Z", "+00:00")) if end else None
    except ValueError:
        return None
    if first is None or last is None:
        return None
    return round(max(0.0, (last - first).total_seconds()), 3)


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


#: A subcommand word: lower case, never an option or a path.
_ACTION_WORD = re.compile(r"[a-z][a-z0-9-]*")


def _command_parts(command: Any) -> tuple[str, str]:
    """The executable's basename and its subcommand, never the argv.

    A recorded command is already the two-token summary ``cli.CliRun.summary``
    writes (``bundle build``), while a failure entry spells ``bundle build
    <stage>``. Either way only a recognisable executable - a path, ``.exe``, a
    ``-cli`` tool - contributes a basename. The subcommand is the first
    remaining token plus a second plain word (``bundle build``,
    ``bundle verify``); options, paths and everything after are dropped.
    """

    text = str(command or "").strip()
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    executable = "prism-kicad"
    if tokens and (
        "/" in tokens[0]
        or "\\" in tokens[0]
        or tokens[0].endswith(_EXECUTABLE_SUFFIXES)
    ):
        executable = os.path.basename(tokens[0])
        tokens = tokens[1:]
    words = tokens[:1]
    if len(tokens) > 1 and _ACTION_WORD.fullmatch(tokens[1]):
        words.append(tokens[1])
    return redact_text(executable), redact_text(" ".join(words))


def _find_bundle(
    project: Any, *, commit: str | None, job_id: str | None
) -> tuple[dict[str, Any] | None, str | None]:
    """The newest bundle for the project and resolved commit, or for this job.

    A job id alone needs no Git resolution: the bundle row already names the
    commit. A commit (or its absence) is resolved exactly as the BOM route does.
    """

    resolved = design_revision.resolve(project, commit) if job_id is None or commit is not None else None
    workspace.initialize()
    where = "project_id = %s"
    values: list[Any] = [_project_id(project)]
    if job_id is not None:
        where += " AND job_id = %s"
        values.append(job_id)
    if resolved is not None:
        where += " AND commit_sha = %s"
        values.append(resolved)
    with rows.default_connect() as conn:
        row = conn.execute(
            f"SELECT * FROM ws_design_bundles WHERE {where} ORDER BY updated_at DESC LIMIT 1",
            tuple(values),
        ).fetchone()
    return (dict(row) if row else None), resolved


def _invocations(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    invocations: list[dict[str, Any]] = []
    for item in metadata.get("invocations") or []:
        if not isinstance(item, dict):
            continue
        executable, subcommand = _command_parts(item.get("command"))
        invocations.append(
            {
                "executable": executable,
                "subcommand": subcommand,
                "stage": redact_text(str(item.get("stage") or "")) or None,
                "exit": _integer(item.get("exit")),
                "elapsed_ms": _number(item.get("elapsed_ms")),
                "peak_rss_bytes": _integer(item.get("peak_rss_bytes")),
            }
        )
    return invocations


def _kicad_steps(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for item in metadata.get("kicad_steps") or []:
        if not isinstance(item, dict):
            continue
        steps.append(
            {
                "step": redact_text(str(item.get("step") or "")),
                "runs": _integer(item.get("runs")) or 0,
                "exit": _integer(item.get("exit")),
                "elapsed_ms": _number(item.get("elapsed_ms")),
                "peak_rss_bytes": _integer(item.get("peak_rss_bytes")),
            }
        )
    return steps


def _slowest(invocations: list[dict[str, Any]], steps: list[dict[str, Any]]) -> str | None:
    """The step or invocation that took longest, named, with its milliseconds dropped."""

    measured: list[tuple[float, str]] = []
    for step in steps:
        if step["elapsed_ms"] is not None and step["step"]:
            measured.append((step["elapsed_ms"], step["step"]))
    for invocation in invocations:
        name = " ".join(
            part
            for part in (invocation["executable"], invocation["subcommand"], invocation["stage"])
            if part
        )
        if invocation["elapsed_ms"] is not None and name:
            measured.append((invocation["elapsed_ms"], name))
    return max(measured, key=lambda item: item[0])[1] if measured else None


def bundle_build_profile(
    project: Any, *, commit: str | None = None, job_id: str | None = None
) -> dict[str, Any] | None:
    """The newest bundle build's per-invocation and per-step profile, or ``None``.

    ``project`` is a workspace row or the ``Project`` model; ``commit`` is
    resolved like any other design read. Reads block; call it from a thread.
    """

    bundle, resolved = _find_bundle(project, commit=commit, job_id=job_id)
    if bundle is None:
        return None

    found_job_id = str(bundle.get("job_id") or "")
    job = jobs.get(found_job_id) if found_job_id else None
    metadata = job.get("result_metadata") if job and isinstance(job.get("result_metadata"), dict) else {}

    stored = metadata.get("stages")
    if not isinstance(stored, dict) or not stored:
        stored = bundle.get("stages") if isinstance(bundle.get("stages"), dict) else {}
    stages = {str(stage): str(status) for stage, status in stored.items()}
    incomplete = manifest_rules.incomplete_stages(stages)
    failed = metadata.get("failed_stages")
    partial = metadata.get("partial_stages")
    failed_stages = (
        [str(stage) for stage in failed if isinstance(stage, str)]
        if isinstance(failed, list)
        else incomplete["failed"]
    )
    partial_stages = (
        [str(stage) for stage in partial if isinstance(stage, str)]
        if isinstance(partial, list)
        else incomplete["partial"]
    )

    invocations = _invocations(metadata)
    steps = _kicad_steps(metadata)
    started_at = job.get("started_at") if job else None
    completed_at = job.get("completed_at") if job else None
    job_status = str(job.get("status") or "") if job else ""

    return {
        "project_id": str(bundle.get("project_id") or _project_id(project)),
        "commit": str(bundle.get("commit_sha") or resolved or "") or None,
        "bundle_key": str(bundle.get("bundle_key") or ""),
        "revision_key": str(bundle.get("revision_key") or ""),
        "design_key": str(bundle.get("design_key") or "") or None,
        "state": str(bundle.get("state") or ""),
        "job_id": found_job_id or None,
        "job_status": job_status or None,
        "archive_bytes": _integer(bundle.get("archive_bytes")) or 0,
        "started_at": started_at,
        "completed_at": completed_at,
        "wall_seconds": _seconds(started_at, completed_at),
        "queue_wait_seconds": _seconds(job.get("created_at"), started_at) if job else None,
        "invocations": invocations,
        "kicad_steps": steps,
        "cache": {
            "upgrade_cache_hits": _integer(metadata.get("upgrade_cache_hits")) or 0,
            "upgrade_cache_misses": _integer(metadata.get("upgrade_cache_misses")) or 0,
            "refill_cache_hit": _flag(metadata.get("refill_cache_hit")),
            "models_cache_hit": _flag(metadata.get("models_cache_hit")),
        },
        "stages": stages,
        "failed_stages": failed_stages,
        "partial_stages": partial_stages,
        "slowest_step": _slowest(invocations, steps),
        "error": redact_text(bundle.get("error") or ""),
    }


__all__ = ["bundle_build_profile"]
