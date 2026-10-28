"""Put a failed or stuck job back in the queue (ADR scripting-and-mcp §11, D21).

An ops action may requeue only a job no worker can finish on its own:

* ``failed`` — the attempt ended and will not run again;
* ``retry_wait`` — a transient failure is waiting for its next attempt, and an
  operator wants it now;
* ``running`` whose lease has expired — no worker owns the attempt any more.

A ``cancel_requested`` job is never requeued, even with an expired lease:
someone asked for it to stop, and lease recovery finalises that cancel.

A running job with a live lease is never requeued: its worker still holds the
row and the claims, so cancellation is the honest action. The update is fenced
on the fence the decision read; a concurrent claim moves the fence and the
requeue refuses rather than overwriting the new owner. ``job_claims`` rows of
the old fence are released in the same transaction, and the event records who
asked and what the row looked like before.
"""

from __future__ import annotations

from typing import Any

from app.core import errors
from app.services import job_claims


class JobNotFound(errors.NotFound):
    """No job carries the id the caller named."""

    code = "job-not-found"
    default_detail = "Job not found"


class JobNotRequeueable(errors.Conflict):
    """The job's current state (or its live lease) forbids a requeue."""

    code = "job-not-requeueable"
    default_detail = "The job cannot be requeued"


def _reason(status: str, *, lease_expired: bool, lease_missing: bool) -> str:
    if status == "cancel_requested":
        return "A cancel was requested for this job; queue a new job instead"
    if status == "running":
        if lease_missing:
            return "The job has no lease to take over; cancel it instead"
        if not lease_expired:
            return (
                "The job is still running with a live lease; cancel it and wait for it "
                "to stop before requeueing it"
            )
    if status == "queued":
        return "The job is already queued"
    if status == "completed":
        return "The job completed successfully; there is nothing to requeue"
    if status == "cancelled":
        return "The job was cancelled deliberately; queue a new job instead"
    return f"A job in state '{status}' cannot be requeued"


def requeue(service: Any, job_id: str, *, requested_by: str = "") -> dict[str, Any]:
    """Reset a requeueable job to ``queued`` under a new fence and report it.

    ``service`` is the :class:`~app.services.job_service.JobService` whose
    connection, event writer and claims this transaction uses. Raises
    :class:`JobNotFound` or :class:`JobNotRequeueable`.
    """

    with service._connect() as conn:
        conn.execute("SET search_path TO workspace, public")
        row = conn.execute(
            """
            SELECT status, fence, attempt, max_attempts,
                   (lease_expires_at IS NULL) AS lease_missing,
                   (lease_expires_at IS NOT NULL AND lease_expires_at < NOW()) AS lease_expired
            FROM ws_jobs
            WHERE id = %s
            FOR UPDATE
            """,
            (job_id,),
        ).fetchone()
        if not row:
            conn.commit()
            raise JobNotFound("Job not found")

        previous_status = str(row["status"])
        lease_expired = bool(row["lease_expired"])
        lease_missing = bool(row["lease_missing"])
        if previous_status not in {"failed", "retry_wait"} and not (
            previous_status == "running" and lease_expired
        ):
            conn.commit()
            raise JobNotRequeueable(
                _reason(
                    previous_status,
                    lease_expired=lease_expired,
                    lease_missing=lease_missing,
                )
            )

        old_fence = int(row["fence"] or 0)
        updated = conn.execute(
            """
            UPDATE ws_jobs
            SET status = 'queued', stage = '', message = 'Requeued by ops',
                available_at = NOW(), fence = fence + 1,
                lease_owner = '', lease_expires_at = NULL, heartbeat_at = NULL,
                error_code = '', error_message = '', completed_at = NULL,
                max_attempts = GREATEST(max_attempts, attempt + 1),
                updated_at = NOW()
            WHERE id = %s AND fence = %s AND status = %s
            RETURNING fence, max_attempts
            """,
            (job_id, old_fence, previous_status),
        ).fetchone()
        if not updated:
            conn.commit()
            raise JobNotRequeueable("The job changed while it was being requeued; try again")

        job_claims.release_claims(conn, job_id, old_fence)
        service._event(
            conn,
            job_id,
            "requeued",
            details={
                "requested_by": requested_by,
                "previous_status": previous_status,
                "previous_fence": old_fence,
            },
        )
        conn.commit()

    return {
        "job_id": job_id,
        "status": "queued",
        "previous_status": previous_status,
        "fence": int(updated["fence"]),
        "previous_fence": old_fence,
        "attempt": int(row["attempt"] or 0),
        "max_attempts": int(updated["max_attempts"]),
    }


__all__ = ["JobNotFound", "JobNotRequeueable", "requeue"]
