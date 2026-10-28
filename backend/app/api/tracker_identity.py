"""Session-user tracker identity OAuth routes (TR-21)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict

from app.core import errors
from app.core.security import AuthenticatedUser, get_current_user, require_admin
from app.services.public_url_service import resolve_public_base_url
from app.services.trackers.errors import ProviderError
from app.services.trackers.identity_service import (
    IdentityNotFound,
    IdentityService,
    OAuthStateError,
    build_oauth_return_url,
    validate_relative_return_to,
)
from app.services.trackers.secrets import SecretStoreLocked

router = APIRouter(prefix="/api/trackers", tags=["tracker-identities"])
admin_router = APIRouter(
    prefix="/api/admin/trackers",
    tags=["tracker-identities-admin"],
    dependencies=[Depends(require_admin)],
)
service = IdentityService()


class OAuthBeginResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authorizeUrl: str


def _actor(user: AuthenticatedUser) -> str:
    return user.user_id or user.email


def _query_return_to(value: object) -> str:
    """Coerce FastAPI query defaults when route handlers are called from unit tests."""

    return value if isinstance(value, str) else "/"


def _oauth_callback_error_code(exc: Exception) -> str | None:
    """Map OAuth callback failures to a query ``tracker_oauth_error`` code.

    Returns ``None`` for unexpected exceptions that should still surface as JSON.
    """

    if isinstance(exc, OAuthStateError):
        code = str(exc)
        if code in {
            "session_expired",
            "cross_user_callback",
            "user_mismatch",
            "unknown_or_reused_state",
            "connector_mismatch",
            "state_expired",
        }:
            return code
        return "invalid_state"
    if isinstance(exc, SecretStoreLocked):
        return "secret_store_locked"
    if isinstance(exc, ProviderError):
        return str(exc.class_ or "provider_error")
    if isinstance(exc, IdentityNotFound):
        return "identity_not_found"
    return None


def _http_error(exc: Exception) -> errors.DomainError:
    if isinstance(exc, IdentityNotFound):
        return errors.NotFound("Identity not found", code="identity_not_found")
    if isinstance(exc, OAuthStateError):
        code = str(exc)
        if code == "session_expired":
            return errors.Conflict(
                "Your session changed during account linking. Sign in again and retry.", code=code,
            )
        if code == "oauth_client_not_configured":
            return errors.Conflict("This code host has no OAuth application registered.", code=code)
        if code == "connector_not_found":
            return errors.NotFound("Code host not found.", code=code)
        if code in {"cross_user_callback", "user_mismatch"}:
            return errors.Forbidden("OAuth callback does not match the signed-in user", code=code)
        return errors.BadRequest("Invalid or expired OAuth state", code="invalid_state")
    if isinstance(exc, SecretStoreLocked):
        return errors.Unavailable("Tracker credential encryption is locked", code="secret_store_locked")
    if isinstance(exc, ProviderError):
        return exc.refusal(409 if exc.class_ == "auth_lost" else 400)
    raise exc


@router.get("/identities")
async def list_identities(user: AuthenticatedUser = Depends(get_current_user)) -> list[dict[str, Any]]:
    if user.auth_type != "session":
        raise HTTPException(status_code=403, detail="Session required to list linked accounts")
    return service.list_identities(_actor(user))


@router.get("/linkable-connectors")
async def list_linkable_connectors(user: AuthenticatedUser = Depends(get_current_user)) -> list[dict[str, Any]]:
    """Code hosts shown under Settings → Connected accounts."""
    if user.auth_type == "kicad_provider":
        raise HTTPException(status_code=403, detail="Remote-symbol tokens cannot list code hosts")
    return service.list_linkable()


@router.post("/connectors/{connector_id}/oauth/begin")
async def begin_oauth(
    connector_id: str,
    request: Request,
    returnTo: str = Query(default="/"),
    user: AuthenticatedUser = Depends(get_current_user),
) -> OAuthBeginResponse:
    if user.auth_type != "session":
        raise HTTPException(status_code=403, detail="Session required to link an account")
    callback_url = oauth_callback_url(request)
    try:
        payload = service.begin_oauth(
            user_id=_actor(user),
            session_id=user.session_id,
            connector_id=connector_id,
            callback_url=callback_url,
            return_to=validate_relative_return_to(_query_return_to(returnTo)),
        )
    except Exception as exc:
        raise _http_error(exc) from exc
    return OAuthBeginResponse(authorizeUrl=payload["authorizeUrl"])


@router.get(
    "/oauth/callback",
    response_class=Response,
    summary="Complete account linking and return the browser to the app",
    responses={302: {"description": "Redirect back to the app with the link result."}},
)
async def oauth_callback(
    request: Request,
    code: str = Query(default=""),
    state: str = Query(default=""),
    user: AuthenticatedUser = Depends(get_current_user),
):
    if user.auth_type != "session":
        raise HTTPException(status_code=403, detail="Session required to complete account linking")
    if not (code or "").strip() or not (state or "").strip():
        target = build_oauth_return_url("/", linked=False, error_code="missing_code_or_state")
        return RedirectResponse(target, status_code=302)
    return_to = service.peek_oauth_return_to(state.strip())
    try:
        identity = service.complete_oauth(
            code=code.strip(),
            state_token=state.strip(),
            session_id=user.session_id,
            actor_user_id=_actor(user),
        )
    except Exception as exc:
        error_code = _oauth_callback_error_code(exc)
        if error_code is None:
            raise _http_error(exc) from exc
        target = build_oauth_return_url(return_to, linked=False, error_code=error_code)
        return RedirectResponse(target, status_code=302)
    return_to = str(identity.pop("returnTo", return_to))
    target = build_oauth_return_url(return_to, linked=True, connector_id=str(identity.get("connectorId") or ""))
    return RedirectResponse(target, status_code=302)


@router.delete("/identities/{connector_id}", status_code=204)
async def unlink_identity(
    connector_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    if user.auth_type != "session":
        raise HTTPException(status_code=403, detail="Session required to unlink an account")
    try:
        service.unlink(_actor(user), connector_id)
    except Exception as exc:
        raise _http_error(exc) from exc


def oauth_callback_url(request: Request) -> str:
    return f"{resolve_public_base_url(request).rstrip('/')}/api/trackers/oauth/callback"


@admin_router.get("/oauth-callback-url")
async def get_oauth_callback_url(request: Request) -> dict[str, str]:
    """The redirect URI an admin registers on each code host's OAuth application."""
    return {"callbackUrl": oauth_callback_url(request)}


@admin_router.post("/connectors/{connector_id}/identities/{user_id}/revoke")
async def admin_revoke_identity(
    connector_id: str,
    user_id: str,
    admin: AuthenticatedUser = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return service.admin_revoke(
            actor_user_id=_actor(admin),
            user_id=user_id,
            connector_id=connector_id,
        )
    except Exception as exc:
        raise _http_error(exc) from exc
