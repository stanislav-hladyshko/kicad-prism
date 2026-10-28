from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from app.core.config import settings
from app.services.job_service import JobService, jobs
from app.services.tree_removal import remove_tree


class JobCancelled(RuntimeError):
    pass


class RetryableJobError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "transient_failure",
        retry_after_seconds: int = 5,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


class PermanentJobError(RuntimeError):
    """A handler failure that must not be retried.

    Codes stay on the job row so callers can distinguish known domain failures
    from an unclassified crash. Unknown catalog errors stay retryable instead.
    """

    def __init__(self, message: str, *, code: str = "handler_failed") -> None:
        super().__init__(message)
        self.code = code


class LostJobLease(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedArtifact:
    kind: str
    artifact_key: str
    digest: str
    object_path: str
    size_bytes: int
    media_type: str = "application/octet-stream"
    schema_version: str = ""
    generator_version: str = ""
    readiness: str = "ready"


@dataclass(frozen=True)
class JobResult:
    result_path: str | None = None
    result_digest: str | None = None
    message: str = "Completed"
    details: Mapping[str, Any] = field(default_factory=dict)
    artifact: PreparedArtifact | None = None
    sidecar_artifacts: tuple[PreparedArtifact, ...] = ()
    # Runs inside the job's completion transaction with its connection, so a
    # feature row (a release build) succeeds exactly when the job completes.
    # It must not use another connection on the job row: that would deadlock.
    settle: Callable[[Any], Any] | None = None


def job_state_root() -> Path:
    configured = settings.PRISM_JOB_ARTIFACT_ROOT.strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (
        Path(settings.KICAD_PROJECTS_ROOT)
        / ".kicad-prism"
    ).expanduser().resolve()


# Payload keys the context owns: resumable state that survives a retry.
CHECKPOINT_KEY = "_checkpoint"
PARTIAL_RESULT_KEY = "_partial_result"
_RESERVED_PAYLOAD_KEYS = frozenset({CHECKPOINT_KEY, PARTIAL_RESULT_KEY})


class JobContext:
    """Fenced execution context passed to one child-process handler.

    Besides progress and cancellation it carries a checkpoint: a handler that
    can resume records ``save_checkpoint(checkpoint=..., result=...)`` as it
    goes, and a retried attempt reads ``checkpoint`` and ``partial_result``
    back. Both live in the job row's payload under keys the context owns, so
    ``payload`` shows the handler only what the caller queued.
    """

    def __init__(
        self,
        job: Mapping[str, Any],
        *,
        worker_id: str,
        service: JobService = jobs,
        progress_interval_seconds: float = 0.5,
    ) -> None:
        self.job = dict(job)
        self.job["payload"] = dict(job.get("payload") or {})
        self.job_id = str(job["job_id"])
        self.fence = int(job["fence"])
        self.worker_id = worker_id
        self.service = service
        self.progress_interval_seconds = max(0.1, progress_interval_seconds)
        self._last_progress_at = 0.0
        self._last_stage = str(job.get("stage") or "")
        self._last_percent = float(job.get("percent") or 0)
        self._pending: dict[str, Any] = {}
        self._progress_lock = threading.RLock()
        self.root = job_state_root()
        self.staging_dir = self.root / "staging" / self.job_id / str(self.fence)
        self.staging_dir.mkdir(parents=True, exist_ok=True)

    @property
    def payload(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.job["payload"].items()
            if key not in _RESERVED_PAYLOAD_KEYS
        }

    @property
    def checkpoint(self) -> dict[str, Any]:
        """The last saved checkpoint, ``{}`` on a first attempt."""

        return dict(self.job["payload"].get(CHECKPOINT_KEY) or {})

    @property
    def partial_result(self) -> dict[str, Any]:
        """The result fields saved so far, accumulated across checkpoints."""

        return dict(self.job["payload"].get(PARTIAL_RESULT_KEY) or {})

    def save_checkpoint(
        self,
        *,
        checkpoint: Mapping[str, Any] | None = None,
        result: Mapping[str, Any] | None = None,
        stage: str | None = None,
        message: str | None = None,
        percent: float | None = None,
    ) -> None:
        """Record resumable state with a progress update; checks cancellation first.

        ``checkpoint`` replaces the saved checkpoint; ``result`` is merged
        into the partial result.
        """

        self.check_cancelled()
        payload = self.job["payload"]
        if checkpoint is not None:
            payload[CHECKPOINT_KEY] = dict(checkpoint)
        if result is not None:
            payload[PARTIAL_RESULT_KEY] = {**self.partial_result, **dict(result)}
        self.progress(
            stage=stage,
            message=message,
            percent=percent,
            payload_updates={
                CHECKPOINT_KEY: self.checkpoint,
                PARTIAL_RESULT_KEY: self.partial_result,
            },
        )

    def check_cancelled(self) -> None:
        current = self.service.get(self.job_id)
        if current is None or int(current.get("fence") or -1) != self.fence:
            raise LostJobLease("Job lease is no longer authoritative")
        if current.get("status") == "cancel_requested":
            raise JobCancelled("Cancellation requested")
        if current.get("status") != "running":
            raise LostJobLease(f"Job is no longer running: {current.get('status')}")

    def progress(
        self,
        *,
        stage: str | None = None,
        message: str | None = None,
        percent: float | None = None,
        payload_updates: Mapping[str, Any] | None = None,
        force: bool = False,
    ) -> None:
        with self._progress_lock:
            now = time.monotonic()
            if stage is not None:
                self._pending["stage"] = stage
            if message is not None:
                self._pending["message"] = message
            if percent is not None:
                self._pending["percent"] = percent
            if payload_updates:
                accumulated = dict(self._pending.get("payload_updates") or {})
                accumulated.update(payload_updates)
                self._pending["payload_updates"] = accumulated
            transition = (
                (stage is not None and stage != self._last_stage)
                or (percent is not None and float(percent) in {0.0, 100.0})
            )
            if (
                not force
                and not transition
                and now - self._last_progress_at < self.progress_interval_seconds
            ):
                return
            self.flush_progress()

    def flush_progress(self) -> None:
        with self._progress_lock:
            if not self._pending:
                return
            values = self._pending
            self._pending = {}
            if not self.service.progress(
                self.job_id,
                self.worker_id,
                self.fence,
                **values,
            ):
                raise LostJobLease("Fenced progress update was rejected")
            self._last_progress_at = time.monotonic()
            if "stage" in values:
                self._last_stage = str(values["stage"])
            if "percent" in values:
                self._last_percent = float(values["percent"])

    def cleanup_staging(self) -> None:
        if self.staging_dir.exists():
            remove_tree(self.staging_dir, ignore_errors=True)


JobHandler = Callable[[JobContext], JobResult]


class JobLogNotFound(LookupError):
    """The job's log is missing or outside the job state root."""


def tail_job_log(path: Path, line_count: int) -> list[str]:
    """The last ``line_count`` lines of a job log under ``job_state_root()/jobs``.

    Reads at most the final 256 KiB. A path outside that root, or a missing
    file, raises ``JobLogNotFound``: a log path comes from the job row, so it is
    checked rather than trusted.
    """

    root = (job_state_root() / "jobs").resolve()
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise JobLogNotFound(str(path)) from error
    if not resolved.is_file():
        raise JobLogNotFound(str(path))
    with resolved.open("rb") as handle:
        size = handle.seek(0, 2)
        handle.seek(max(0, size - 256 * 1024))
        text = handle.read(256 * 1024).decode("utf-8", errors="replace")
    return text.splitlines()[-line_count:]
