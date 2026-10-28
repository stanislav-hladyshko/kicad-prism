"""End job attempts no worker can finish, before a claim looks for work.

Both run inside the claim's transaction, for the claiming worker's pool:

* a ``cancel_requested`` attempt whose lease expired is cancelled, so it no
  longer blocks dedup or holds resources;
* a ``running`` or ``cancel_requested`` job with no lease at all is ended.
  Every claim sets a lease in the same statement that makes a job
  ``running``, so such a row predates leases (or was edited by hand).
  Expired-lease reclaiming only matches rows with a lease, so these stayed
  active forever, blocking dedup for their artifact key. They are failed
  rather than retried: whatever enqueued them may no longer exist.

Expired ``running`` attempts are not ended here; the claim reclaims them.

Each function returns the jobs it ended as ``(job_id, kind, project_id,
state)``, so the claim can append ``job.finished`` once its transaction commits,
as every other way a job ends does.
"""

from __future__ import annotations

from typing import Any, Callable

ReleaseClaims = Callable[[Any, str, int], None]
RecordEvent = Callable[..., None]
#: ``(job_id, kind, project_id, terminal state)`` for one ended job.
Ended = tuple[str, str, Any, str]


def recover_dead_attempts(
    conn: Any, worker_pool: str, release_claims: ReleaseClaims, record_event: RecordEvent
) -> list[Ended]:
    return [
        *finalize_expired_cancellations(conn, worker_pool, release_claims, record_event),
        *end_unleased_attempts(conn, worker_pool, release_claims, record_event),
    ]


def finalize_expired_cancellations(
    conn: Any, worker_pool: str, release_claims: ReleaseClaims, record_event: RecordEvent
) -> list[Ended]:
    """Fence dead cancelling attempts so they cannot block dedup or resources."""

    rows = conn.execute(
        """
        SELECT id, fence, kind, project_id
        FROM ws_jobs
        WHERE worker_pool = %s
          AND status = 'cancel_requested'
          AND lease_expires_at IS NOT NULL
          AND lease_expires_at < NOW()
        ORDER BY created_at, id
        FOR UPDATE SKIP LOCKED
        LIMIT 128
        """,
        (worker_pool,),
    ).fetchall()
    ended: list[Ended] = []
    for row in rows:
        job_id = str(row["id"])
        old_fence = int(row["fence"] or 0)
        updated = conn.execute(
            """
            UPDATE ws_jobs
            SET status = 'cancelled', stage = 'cancelled',
                message = 'Cancelled after worker lease expired',
                fence = fence + 1, completed_at = NOW(),
                heartbeat_at = NOW(), lease_owner = '',
                lease_expires_at = NULL, updated_at = NOW()
            WHERE id = %s AND fence = %s AND status = 'cancel_requested'
            """,
            (job_id, old_fence),
        )
        release_claims(conn, job_id, old_fence)
        record_event(
            conn,
            job_id,
            "cancelled_after_lease_expiry",
            details={"previous_fence": old_fence, "fence": old_fence + 1},
        )
        if updated.rowcount == 1:
            ended.append((job_id, str(row["kind"]), row["project_id"], "cancelled"))
    return ended


def end_unleased_attempts(
    conn: Any, worker_pool: str, release_claims: ReleaseClaims, record_event: RecordEvent
) -> list[Ended]:
    """End active jobs that hold no lease, which no worker can own or reclaim."""

    rows = conn.execute(
        """
        SELECT id, fence, status, kind, project_id
        FROM ws_jobs
        WHERE worker_pool = %s
          AND status IN ('running', 'cancel_requested')
          AND lease_expires_at IS NULL
        ORDER BY created_at, id
        FOR UPDATE SKIP LOCKED
        LIMIT 128
        """,
        (worker_pool,),
    ).fetchall()
    ended: list[Ended] = []
    for row in rows:
        job_id = str(row["id"])
        old_fence = int(row["fence"] or 0)
        cancelling = row["status"] == "cancel_requested"
        updated = conn.execute(
            """
            UPDATE ws_jobs
            SET status = %s, stage = %s, message = %s,
                error_code = CASE WHEN %s THEN error_code ELSE 'orphaned' END,
                error_message = CASE WHEN %s THEN error_message ELSE %s END,
                fence = fence + 1, completed_at = NOW(),
                lease_owner = '', updated_at = NOW()
            WHERE id = %s AND fence = %s
              AND status = %s AND lease_expires_at IS NULL
            """,
            (
                "cancelled" if cancelling else "failed",
                "cancelled" if cancelling else "failed",
                "Cancelled: no worker held this job" if cancelling else "No worker held this job",
                cancelling,
                cancelling,
                "The job was active without a worker lease, so no worker could finish or reclaim it.",
                job_id,
                old_fence,
                row["status"],
            ),
        )
        release_claims(conn, job_id, old_fence)
        record_event(
            conn,
            job_id,
            "ended_without_lease",
            details={"previous_status": row["status"], "previous_fence": old_fence, "fence": old_fence + 1},
        )
        if updated.rowcount == 1:
            state = "cancelled" if cancelling else "failed"
            ended.append((job_id, str(row["kind"]), row["project_id"], state))
    return ended
