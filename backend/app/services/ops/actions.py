"""The fixed ops actions (ADR scripting-and-mcp §11, D21; OPS-06).

Four actions, and no more: cancel one job, requeue a failed or stuck one,
rebuild one design bundle, and raise logging to DEBUG for at most thirty
minutes. Each runs the same service the browser calls and reports what it did;
none accepts a shell command, SQL or a configuration value. Free text the
routes return passes through ``app.core.redaction`` (D22).
"""

from __future__ import annotations

from typing import Any

from app.core import errors, redaction, runtime_logging
from app.services import job_requeue
from app.services.design_bundle import cli, regenerate
from app.services.job_service import jobs

#: The most a debug window may last, stated where callers and tools see it.
MAX_DEBUG_MINUTES = runtime_logging.MINUTES_MAX

#: A debug window may name only Prism's own loggers: ``app`` and its children.
LOGGER_PATTERN = r"^app(\.[A-Za-z0-9_]+)*$"


def cancel_job(job_id: str, *, requested_by: str = "") -> dict[str, Any]:
    """Ask a job to cancel and return the status the request left it in."""

    status = jobs.request_cancel(job_id, requested_by=requested_by)
    if status is None:
        raise job_requeue.JobNotFound("Job not found")
    return {"job_id": job_id, "status": status}


def requeue_job(job_id: str, *, requested_by: str = "") -> dict[str, Any]:
    """Requeue a failed, retry-waiting or lease-expired job."""

    return jobs.requeue(job_id, requested_by=requested_by)


def rebuild_bundle(
    project_id: str, *, commit: str | None = None, requested_by: str = ""
) -> dict[str, Any]:
    """Drop one commit's cached bundle and queue a rebuild that skips reuse."""

    try:
        result = regenerate.regenerate(
            project_id, commit=commit, scope="commit", requested_by=requested_by
        )
    except regenerate.RegenerateConflict as error:
        raise errors.Conflict(
            "A design bundle of this project is being built; try again when it finishes",
            code="bundle-build-active",
        ) from error
    except regenerate.RegenerateNotApplicable as error:
        raise errors.NotFound(
            "The project is not present at this commit", code="bundle-not-applicable"
        ) from error
    except LookupError as error:
        raise errors.NotFound("Project not found", code="project-not-found") from error
    except cli.CliUnavailable as error:
        raise errors.Unavailable(
            "The design bundle builder is not available", code="cli-unavailable"
        ) from error

    return {
        "job_id": result.job_id,
        "commit": result.commit,
        "scope": result.scope,
        "removed_bundles": result.removed_bundles,
        "removed_aliases": result.removed_aliases,
    }


def set_debug_logging(
    minutes: int, *, loggers: list[str] | None = None, by: str = ""
) -> dict[str, Any]:
    """Store a bounded DEBUG window and return what was stored."""

    try:
        value = runtime_logging.set_debug(minutes, loggers=loggers, by=by)
    except ValueError as error:
        raise errors.Invalid(
            f"minutes must be between {runtime_logging.MINUTES_MIN} and {MAX_DEBUG_MINUTES}",
            code="invalid-debug-window",
        ) from error
    return _window(value)


def current_debug_logging() -> dict[str, Any] | None:
    """The active debug window, or ``None`` when there is none."""

    return _window(runtime_logging.current())


def clear_debug_logging(*, by: str = "") -> None:
    """Remove the debug window; every process reverts on its next poll."""

    runtime_logging.clear(by)


def _window(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "until": redaction.redact_text(value.get("until") or ""),
        "loggers": [
            redaction.redact_text(name) for name in value.get("loggers") or []
        ],
    }


__all__ = [
    "LOGGER_PATTERN",
    "MAX_DEBUG_MINUTES",
    "cancel_job",
    "clear_debug_logging",
    "current_debug_logging",
    "rebuild_bundle",
    "requeue_job",
    "set_debug_logging",
]
