"""One delivery pass over the event log (ADR §3.10, v5.2).

The worker calls :func:`run_pass` from a daemon thread every five seconds. Each
active registration reads the next events after its cursor, in log order, and
posts the ones its kind and project filters admit:

- a 2xx advances the cursor and clears the retry state;
- a failure records a delivery row, stops that registration for this pass, and
  schedules ``min(30 s * 2^(attempts-1), 1 h)``. After 24 hours of failures the
  registration is marked ``failing`` and stays quiet until an administrator
  re-enables it;
- an event the filters skip still advances the cursor, so it is not reconsidered
  on every pass.

A pass also prunes delivery rows older than 30 days in one bounded delete.
Nothing here runs on an event loop: the worker thread owns the blocking database
and HTTP calls.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from urllib.parse import urlsplit

import urllib3

from app.core.errors import Conflict, DomainError
from app.services.trackers.secrets import SecretStoreLocked
from app.services.webhooks import signing, store, targets

RETRY_BASE_SECONDS = 30
RETRY_MAX_SECONDS = 3600
FAILING_AFTER = timedelta(hours=24)
EVENTS_PER_PASS = 50
SEND_TIMEOUT_SECONDS = 10
DELIVERY_RETENTION_DAYS = 30

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SendOutcome:
    """What one HTTP attempt did: status, success, a short error, how long."""

    status_code: int | None
    ok: bool
    error: str | None
    duration_ms: int


Sender = Callable[[str, bytes, Mapping[str, str]], SendOutcome]


def http_sender(url: str, body: bytes, headers: Mapping[str, str]) -> SendOutcome:
    """The production sender: one POST, ten seconds, never following redirects.

    The connection goes to the address ``targets`` just checked, with TLS still
    verified against the URL's own host name (as ``mcp_clients`` fetches do).
    """

    started = time.monotonic()
    try:
        address = targets.checked_address(url)
    except DomainError as exc:
        return SendOutcome(None, False, str(exc.detail), _elapsed_ms(started))
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    timeout = urllib3.Timeout(total=SEND_TIMEOUT_SECONDS)
    if parsed.scheme == "https":
        pool: urllib3.HTTPConnectionPool = urllib3.HTTPSConnectionPool(
            address,
            port=parsed.port or 443,
            server_hostname=host,
            assert_hostname=host,
            cert_reqs="CERT_REQUIRED",
            timeout=timeout,
            retries=False,
        )
    else:  # only reachable with PRISM_WEBHOOK_ALLOW_PRIVATE
        pool = urllib3.HTTPConnectionPool(address, port=parsed.port or 80, timeout=timeout, retries=False)
    path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
    try:
        response = pool.request(
            "POST",
            path,
            body=body,
            headers={**dict(headers), "Host": parsed.netloc},
            redirect=False,
        )
    except urllib3.exceptions.HTTPError as exc:
        return SendOutcome(None, False, _error_text(exc), _elapsed_ms(started))
    finally:
        pool.close()
    status = int(response.status)
    ok = 200 <= status < 300
    return SendOutcome(
        status,
        ok,
        None if ok else f"HTTP {status}",
        _elapsed_ms(started),
    )


#: Several workers may serve the ``prism`` pool; one pass at a time across all
#: of them, or each would post every event.
PASS_LOCK_KEY = 0x5052_4953_4D57_4831  # "PRISMWH1"


def run_pass(*, sender: Sender | None = None, now: datetime | None = None) -> dict[str, int]:
    """Deliver every due registration once, then prune old delivery rows.

    A pass that finds another worker's pass running does nothing.
    """

    with store.connect() as conn:
        locked = conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (PASS_LOCK_KEY,)).fetchone()
        if not (locked["locked"] if isinstance(locked, Mapping) else locked[0]):
            return {"webhooks": 0, "delivered": 0, "failed": 0, "pruned": 0}
        try:
            return _run_pass(sender=sender, now=now)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (PASS_LOCK_KEY,))


def _run_pass(*, sender: Sender | None, now: datetime | None) -> dict[str, int]:
    moment = now or datetime.now(timezone.utc)
    chosen = sender or http_sender
    stats = {"webhooks": 0, "delivered": 0, "failed": 0, "pruned": 0}
    for webhook in store.due(moment):
        stats["webhooks"] += 1
        try:
            delivered, failed = deliver_webhook(webhook, sender=chosen, now=moment)
        except Exception:
            # One broken registration must not starve the others this pass.
            logger.exception("Webhook %s could not be delivered", webhook.get("id"))
            stats["failed"] += 1
            continue
        stats["delivered"] += delivered
        stats["failed"] += 1 if failed else 0
    stats["pruned"] = store.prune_deliveries(DELIVERY_RETENTION_DAYS)
    return stats


def deliver_webhook(
    webhook: dict[str, Any], *, sender: Sender, now: datetime
) -> tuple[int, bool]:
    """Settle as many events as one pass allows; return (delivered, stopped)."""

    try:
        secret = store.decrypt(dict(webhook))
    except SecretStoreLocked:
        store.record_delivery(
            str(webhook["id"]),
            event_id=None,
            status_code=None,
            ok=False,
            error="webhook secret is unavailable",
            duration_ms=0,
        )
        _fail(
            webhook,
            status_code=None,
            error="webhook secret is unavailable",
            now=now,
        )
        return 0, True

    delivered = 0
    for event in store.events_after(int(webhook["cursor"]), limit=EVENTS_PER_PASS):
        if not _matches(webhook, event):
            store.save_cursor(str(webhook["id"]), int(event["id"]))
            continue
        outcome = _attempt(
            webhook,
            secret,
            signing.event_body(event),
            str(event["id"]),
            sender,
            now,
        )
        store.record_delivery(
            str(webhook["id"]),
            event_id=int(event["id"]),
            status_code=outcome.status_code,
            ok=outcome.ok,
            error=outcome.error,
            duration_ms=outcome.duration_ms,
        )
        if not outcome.ok:
            _fail(
                webhook,
                status_code=outcome.status_code,
                error=outcome.error,
                now=now,
            )
            return delivered, True
        store.record_success(
            str(webhook["id"]),
            event_id=int(event["id"]),
            status_code=outcome.status_code,
            at=now,
        )
        # A success resets the retry state, so a later failure in this same
        # pass starts the backoff from zero rather than from a stale count.
        webhook["attempts"] = 0
        webhook["failing_since"] = None
        delivered += 1
    return delivered, False


def send_ping(
    webhook_id: str, *, sender: Sender | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Send one synchronous ``ping`` and return its outcome for Settings."""

    moment = now or datetime.now(timezone.utc)
    chosen = sender or http_sender
    webhook = store.get_row(webhook_id)
    try:
        secret = store.decrypt(webhook)
    except SecretStoreLocked as exc:
        raise Conflict(
            "Webhook secrets need TRACKER_CREDENTIAL_ROOT_KEY configured",
            code=store.SECRET_STORE_LOCKED,
        ) from exc

    event_id = str(uuid.uuid4())
    body = signing.ping_body(event_id=event_id, created_at=_isoformat(moment))
    outcome = _attempt(webhook, secret, body, event_id, chosen, moment)
    store.record_delivery(
        str(webhook["id"]),
        event_id=None,
        status_code=outcome.status_code,
        ok=outcome.ok,
        error=outcome.error,
        duration_ms=outcome.duration_ms,
    )
    return {
        "statusCode": outcome.status_code,
        "ok": outcome.ok,
        "error": outcome.error,
        "durationMs": outcome.duration_ms,
    }


def _attempt(
    webhook: Mapping[str, Any],
    secret: str,
    body: bytes,
    webhook_id: str,
    sender: Sender,
    now: datetime,
) -> SendOutcome:
    """Check the target again, then send; any refusal is a failed attempt."""

    try:
        targets.require_target(str(webhook["url"]))
    except DomainError as exc:
        return SendOutcome(None, False, str(exc.detail), 0)
    headers = signing.headers(secret, webhook_id, body, int(now.timestamp()))
    started = time.monotonic()
    try:
        return sender(str(webhook["url"]), body, headers)
    except Exception as exc:  # a pluggable sender must not take the pass down
        return SendOutcome(None, False, _error_text(exc), _elapsed_ms(started))


def _fail(
    webhook: Mapping[str, Any],
    *,
    status_code: int | None,
    error: str | None,
    now: datetime,
) -> None:
    """Record one failed attempt and the retry it schedules."""

    attempts = int(webhook["attempts"] or 0) + 1
    failing_since = webhook["failing_since"] or now
    status = "failing" if now - failing_since > FAILING_AFTER else "active"
    delay = min(RETRY_BASE_SECONDS * 2 ** (attempts - 1), RETRY_MAX_SECONDS)
    store.record_failure(
        str(webhook["id"]),
        attempts=attempts,
        failing_since=failing_since,
        next_attempt_at=now + timedelta(seconds=delay),
        status=status,
        status_code=status_code,
        error=error,
        at=now,
    )


def _matches(webhook: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    if event["kind"] not in (webhook["event_kinds"] or []):
        return False
    project_ids = webhook["project_ids"]
    if project_ids is None:
        return True
    return event.get("projectId") is not None and event["projectId"] in project_ids


def _error_text(error: BaseException) -> str:
    text = str(error).strip()
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _isoformat(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


__all__ = [
    "EVENTS_PER_PASS",
    "FAILING_AFTER",
    "RETRY_BASE_SECONDS",
    "RETRY_MAX_SECONDS",
    "SendOutcome",
    "Sender",
    "deliver_webhook",
    "http_sender",
    "run_pass",
    "send_ping",
]
