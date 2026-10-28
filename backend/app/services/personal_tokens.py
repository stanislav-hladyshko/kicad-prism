"""Scoped personal access tokens for scripts and CI (ADR scripting-and-mcp §3.6).

A person creates a token in Settings, names it, chooses scopes and an expiry,
and sees the secret once. Only its SHA-256 hex digest is stored, so a database
read cannot recover a working credential. Validation re-reads the owner's role
on every request: a token never holds more than its user can do now, and a
removed user stops working immediately.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from typing import Any

from psycopg.types.json import Jsonb

from app.core import policy
from app.core.config import settings
from app.core.errors import BadRequest, NotFound
from app.services.postgres_database import database

TOKEN_PREFIX = "prism_pat_"
TOKEN_RANDOM_LENGTH = 40
# "prism_pat_" plus two characters: enough to recognise a token, far too little to use it.
DISPLAY_PREFIX_LENGTH = 12

_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
# The remote-symbol scope belongs to the KiCad provider, and "*" would sidestep the
# scope table entirely; neither is offered to a person's token.
ALLOWED_SCOPES = policy.KNOWN_SCOPES - {"remote_symbols.read"}

_COLUMNS = (
    "id, user_email, name, scopes, prefix, created_at, expires_at,"
    " last_used_at, revoked_at"
)


def _new_token() -> str:
    return TOKEN_PREFIX + "".join(
        secrets.choice(_BASE62) for _ in range(TOKEN_RANDOM_LENGTH)
    )


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _normalize_scopes(scopes: list[str]) -> list[str]:
    requested: list[str] = []
    for value in scopes:
        scope = str(value).strip()
        if scope and scope not in requested:
            requested.append(scope)
    if not requested:
        raise BadRequest("At least one scope is required")
    unsupported = [scope for scope in requested if scope not in ALLOWED_SCOPES]
    if unsupported:
        raise BadRequest(f"Unsupported scope: {', '.join(sorted(unsupported))}")
    return requested


def _validate_days(days: int) -> int:
    if not 1 <= days <= settings.PRISM_PAT_MAX_DAYS:
        raise BadRequest(f"Expiry must be between 1 and {settings.PRISM_PAT_MAX_DAYS} days")
    return days


def create(
    email: str, name: str, scopes: list[str], days: int
) -> tuple[dict[str, Any], str]:
    """Mint a token for ``email``; return its row and the secret, which is shown once."""

    normalized_email = _normalize_email(email)
    if not normalized_email:
        raise BadRequest("A signed-in user is required")
    label = (name or "").strip()
    if not 1 <= len(label) <= 80:
        raise BadRequest("Name must be between 1 and 80 characters")
    requested_scopes = _normalize_scopes(scopes)
    _validate_days(days)

    token = _new_token()
    token_id = "pat_" + secrets.token_hex(8)
    with database.connection() as connection:
        connection.execute("SET search_path TO workspace, public")
        row = connection.execute(
            f"""
            INSERT INTO ws_personal_access_tokens
                (id, user_email, name, scopes, token_hash, prefix, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW() + make_interval(days => %s))
            RETURNING {_COLUMNS}
            """,
            (
                token_id,
                normalized_email,
                label,
                Jsonb(requested_scopes),
                hash_token(token),
                token[:DISPLAY_PREFIX_LENGTH],
                days,
            ),
        ).fetchone()
        connection.commit()
    return dict(row), token


def list_for(email: str) -> list[dict[str, Any]]:
    """The caller's own tokens, newest first."""

    with database.connection() as connection:
        connection.execute("SET search_path TO workspace, public")
        rows = connection.execute(
            f"""
            SELECT {_COLUMNS} FROM ws_personal_access_tokens
            WHERE user_email = %s ORDER BY created_at DESC, id DESC
            """,
            (_normalize_email(email),),
        ).fetchall()
    return [dict(row) for row in rows]


def list_all() -> list[dict[str, Any]]:
    """Every person's tokens, for an administrator."""

    with database.connection() as connection:
        connection.execute("SET search_path TO workspace, public")
        rows = connection.execute(
            f"""
            SELECT {_COLUMNS} FROM ws_personal_access_tokens
            ORDER BY created_at DESC, id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def revoke(token_id: str, *, by_email: str, is_admin: bool) -> dict[str, Any]:
    """Revoke a token its owner or an administrator may see.

    A token that is not the caller's is reported as not found, so the route cannot
    be used to discover other people's token ids.
    """

    with database.connection() as connection:
        connection.execute("SET search_path TO workspace, public")
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM ws_personal_access_tokens WHERE id = %s",
            (token_id,),
        ).fetchone()
        if row is None or (
            not is_admin and str(row["user_email"]).strip().lower() != _normalize_email(by_email)
        ):
            raise NotFound("Token not found")
        revoked = connection.execute(
            f"""
            UPDATE ws_personal_access_tokens
            SET revoked_at = COALESCE(revoked_at, NOW())
            WHERE id = %s
            RETURNING {_COLUMNS}
            """,
            (token_id,),
        ).fetchone()
        connection.commit()
    return dict(revoked)


def validate(token: str) -> dict[str, Any] | None:
    """The live row for ``token``, or ``None`` when it is unknown, revoked or expired."""

    if not token.startswith(TOKEN_PREFIX):
        return None
    with database.connection() as connection:
        connection.execute("SET search_path TO workspace, public")
        row = connection.execute(
            f"""
            SELECT {_COLUMNS} FROM ws_personal_access_tokens
            WHERE token_hash = %s AND revoked_at IS NULL AND expires_at > NOW()
            """,
            (hash_token(token),),
        ).fetchone()
    return dict(row) if row else None


# A "last used" column does not need to be exact. Writing on every request would
# put a database round-trip in the hot path of every script call, so refresh at
# most once a minute per token, like ``agent_auth_service.touch_agent_token``.
_TOUCH_THROTTLE_SECONDS = 60
_TOUCH_CACHE_MAX = 4096
_last_touched: dict[str, int] = {}
_touch_lock = threading.Lock()


def _now() -> int:
    return int(time.time())


def _touch_store(token_id: str) -> None:
    with database.connection() as connection:
        connection.execute("SET search_path TO workspace, public")
        connection.execute(
            "UPDATE ws_personal_access_tokens SET last_used_at = NOW() WHERE id = %s",
            (token_id,),
        )
        connection.commit()


def touch(row: dict[str, Any]) -> None:
    """Record that a token was just used, throttled per token. Never fails a request."""

    token_id = str(row.get("id") or "")
    if not token_id:
        return
    now = _now()
    with _touch_lock:
        if now - _last_touched.get(token_id, 0) < _TOUCH_THROTTLE_SECONDS:
            return
        if len(_last_touched) >= _TOUCH_CACHE_MAX:
            stale = [
                key
                for key, seen in _last_touched.items()
                if now - seen >= _TOUCH_THROTTLE_SECONDS
            ]
            for key in stale:
                del _last_touched[key]
            if not stale:
                _last_touched.clear()
        _last_touched[token_id] = now
    try:
        _touch_store(token_id)
    except Exception:  # noqa: BLE001 - a bookkeeping write must not fail a request
        pass
