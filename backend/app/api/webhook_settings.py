"""Admin settings for outbound webhooks (ADR §3.10, v5.2).

An administrator registers an HTTPS endpoint that receives the event log's
events. The signing secret is returned by create and rotate-secret only; every
other response is a view without it. Blocks database work in a worker thread, and
the synchronous test ping in a thread too, so no request holds the event loop on
HTTP.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from app.core.errors import Invalid
from app.core.security import AuthenticatedUser, require_admin
from app.services.webhooks import delivery, store

router = APIRouter(prefix="/api/settings/webhooks", tags=["settings"])


class WebhookView(BaseModel):
    """A registration as Settings shows it: no secret, no envelope."""

    id: str
    url: str
    eventKinds: list[str]
    projectIds: list[str] | None = None
    status: Literal["active", "failing", "disabled"]
    cursor: int
    attempts: int
    failingSince: datetime | None = None
    nextAttemptAt: datetime | None = None
    lastStatus: int | None = None
    lastError: str | None = None
    lastDeliveryAt: datetime | None = None
    createdBy: str | None = None
    createdAt: datetime


class CreatedWebhook(WebhookView):
    """The one response that carries the plaintext signing secret."""

    secret: str


class WebhookSecret(BaseModel):
    """A rotated secret, shown once."""

    secret: str


class WebhookDeliveryView(BaseModel):
    id: int
    eventId: int | None = None
    at: datetime
    statusCode: int | None = None
    ok: bool
    error: str | None = None
    durationMs: int | None = None


class WebhookTestResult(BaseModel):
    statusCode: int | None = None
    ok: bool
    error: str | None = None
    durationMs: int


class WebhookCreate(BaseModel):
    url: str
    eventKinds: list[str]
    projectIds: list[str] | None = None


class WebhookUpdate(BaseModel):
    url: str | None = None
    eventKinds: list[str] | None = None
    projectIds: list[str] | None = None
    status: Literal["active", "disabled"] | None = None


@router.get("", response_model=list[WebhookView], summary="List outbound webhooks")
async def list_webhooks(
    _user: AuthenticatedUser = Depends(require_admin),
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(store.list_all)


@router.post("", response_model=CreatedWebhook, summary="Create an outbound webhook")
async def create_webhook(
    body: WebhookCreate,
    user: AuthenticatedUser = Depends(require_admin),
) -> dict[str, Any]:
    """Register an endpoint; the returned secret is shown once."""
    row, secret = await asyncio.to_thread(
        store.create,
        url=body.url,
        event_kinds=body.eventKinds,
        project_ids=body.projectIds,
        created_by=user.email,
    )
    return {**row, "secret": secret}


@router.patch("/{webhook_id}", response_model=WebhookView, summary="Edit an outbound webhook")
async def update_webhook(
    webhook_id: str,
    body: WebhookUpdate,
    _user: AuthenticatedUser = Depends(require_admin),
) -> dict[str, Any]:
    """Change the target, filters or status; ``projectIds: null`` means all projects."""
    fields = body.model_fields_set
    changes: dict[str, Any] = {}
    if "url" in fields:
        changes["url"] = _required(body.url, "url")
    if "eventKinds" in fields:
        changes["eventKinds"] = _required(body.eventKinds, "eventKinds")
    if "projectIds" in fields:
        changes["projectIds"] = body.projectIds
    if "status" in fields:
        changes["status"] = _required(body.status, "status")
    kwargs = {
        "url": changes.get("url", store.UNSET),
        "event_kinds": changes.get("eventKinds", store.UNSET),
        "project_ids": changes.get("projectIds", store.UNSET),
        "status": changes.get("status", store.UNSET),
    }
    return await asyncio.to_thread(store.update, webhook_id, **kwargs)


@router.delete("/{webhook_id}", status_code=204, response_model=None)
async def delete_webhook(
    webhook_id: str,
    _user: AuthenticatedUser = Depends(require_admin),
) -> Response:
    await asyncio.to_thread(store.delete, webhook_id)
    return Response(status_code=204)


@router.post(
    "/{webhook_id}/rotate-secret",
    response_model=WebhookSecret,
    summary="Rotate an outbound webhook's signing secret",
)
async def rotate_webhook_secret(
    webhook_id: str,
    _user: AuthenticatedUser = Depends(require_admin),
) -> dict[str, Any]:
    _row, secret = await asyncio.to_thread(store.rotate_secret, webhook_id)
    return {"secret": secret}


@router.post(
    "/{webhook_id}/test",
    response_model=WebhookTestResult,
    summary="Send a test ping to an outbound webhook",
)
async def test_webhook(
    webhook_id: str,
    _user: AuthenticatedUser = Depends(require_admin),
) -> dict[str, Any]:
    """Send one synchronous ``ping``; it never enters the event log."""
    return await asyncio.to_thread(delivery.send_ping, webhook_id)


@router.get(
    "/{webhook_id}/deliveries",
    response_model=list[WebhookDeliveryView],
    summary="List an outbound webhook's latest deliveries",
)
async def list_webhook_deliveries(
    webhook_id: str,
    _user: AuthenticatedUser = Depends(require_admin),
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(store.list_deliveries, webhook_id, limit=50)


def _required(value: Any, field: str) -> Any:
    if value is None:
        raise Invalid(f"{field} is required", code="invalid_field")
    return value


__all__ = ["router"]
